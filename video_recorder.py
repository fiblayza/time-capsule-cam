import os
import subprocess
import json
import time
import yaml
import logging
import signal
import sys
import shutil
import threading
from datetime import datetime
from pathlib import Path

import wifi_qr

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False
    logging.warning("RPi.GPIO not available — running in simulation mode")

try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import FfmpegOutput
    PICAMERA2_AVAILABLE = True
except ImportError:
    PICAMERA2_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# Newer upstream images (Dec 2025+, trixie) install to /opt; older ones to /home/admin
_UPSTREAM_CANDIDATES = [
    Path("/opt/rotary-phone-audio-guestbook"),
    Path(__file__).parent.parent / "rotary-phone-audio-guestbook",
]
UPSTREAM_DIR = next((p for p in _UPSTREAM_CANDIDATES if p.is_dir()), _UPSTREAM_CANDIDATES[-1])
CONFIG_PATH = UPSTREAM_DIR / "config.yaml"
RECORDINGS_DIR_FALLBACK = UPSTREAM_DIR / "recordings"
STATUS_PATH = Path(__file__).parent / "status.json"
SOUNDS_DIR = Path(__file__).parent / "sounds"

# time.monotonic() counts from power-on: a handset already lifted this early
# means "plugged in with the handset up" (wifi setup), not a mid-call restart
BOOT_WINDOW_SECONDS = 60
WIFI_SETUP_TIMEOUT_SECONDS = 120

_last_stop_time = 0.0       # monotonic time of last stop, for cooldown
_recording_proc = None       # ffmpeg subprocess
_picamera = None             # picamera2 instance
_recording_start = None      # epoch float
_current_file = None         # Path of the file being written
_led_pin = None              # BCM pin for recording LED (None = disabled)
_limit_timer = None          # threading.Timer that stops runaway recordings
_wav_detected_at = None      # monotonic instant the audio .wav appeared
_av_offset = 0.0             # seconds the camera started after the audio did
_lock = threading.Lock()     # hook callback and limit timer can race on stop

# ponytail: single append-only log shared by all ffmpeg runs; rotate by hand if it ever matters
_FFMPEG_LOG = open(Path(__file__).parent / "ffmpeg.log", "ab")


def set_status(state: str):
    STATUS_PATH.write_text(json.dumps({"status": state}))
    log.info("Status → %s", state)


def set_led(on: bool):
    if GPIO_AVAILABLE and _led_pin is not None:
        GPIO.output(_led_pin, GPIO.HIGH if on else GPIO.LOW)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    # Fallback defaults when running outside the Pi environment
    log.warning("config.yaml not found at %s — using defaults", CONFIG_PATH)
    return {
        "hook_gpio": 22,
        "hook_type": "NC",
        "recordings_path": str(RECORDINGS_DIR_FALLBACK),
        "video": {
            "enabled": True,
            "backend": "usb",
            "device": "/dev/video0",
            "resolution": "1280x720",
            "fps": 25,
            "min_duration_seconds": 2,
            "min_gap_seconds": 1.0,
            "codec": "libx264",
            "preset": "ultrafast",
            "led_gpio": 17,
        },
    }


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def start_recording(cfg: dict):
    with _lock:
        _start_recording_locked(cfg)


def _start_recording_locked(cfg: dict):
    global _recording_proc, _picamera, _recording_start, _current_file, _limit_timer

    if _recording_proc is not None or _picamera is not None:
        log.warning("start_recording called while already recording — ignoring")
        return

    recordings_dir = Path(cfg.get("recordings_path", str(RECORDINGS_DIR_FALLBACK)))
    recordings_dir.mkdir(parents=True, exist_ok=True)
    ts = _timestamp()
    _current_file = recordings_dir / f"{ts}.mp4"
    _recording_start = time.monotonic()

    vcfg = cfg.get("video", {})
    backend = vcfg.get("backend", "usb")

    if backend == "picamera":
        if not PICAMERA2_AVAILABLE:
            log.error("picamera2 not installed — cannot record")
            _current_file = None
            _recording_start = None
            return
        width, height = map(int, vcfg.get("resolution", "1280x720").split("x"))
        fps = int(vcfg.get("fps", 25))
        _picamera = Picamera2()
        video_config = _picamera.create_video_configuration(
            main={"size": (width, height)},
            controls={"FrameRate": fps},
        )
        _picamera.configure(video_config)
        encoder = H264Encoder(bitrate=2_000_000)
        # FfmpegOutput wraps the stream in a real MP4 container — FileOutput
        # wrote a bare H.264 bitstream that browsers can't play. Fragmented
        # (same flags as the usb backend) so a crash doesn't corrupt the file.
        # picamera2 splits this string, so options can ride along with the path.
        output = FfmpegOutput(f"-movflags +frag_keyframe+empty_moov {_current_file}")
        _picamera.start_recording(encoder, output)
        log.info("picamera2 recording → %s", _current_file)
    else:
        # USB webcam via ffmpeg
        device = vcfg.get("device", "/dev/video0")
        resolution = vcfg.get("resolution", "1280x720")
        fps = str(vcfg.get("fps", 25))
        codec = vcfg.get("codec", "libx264")
        preset = vcfg.get("preset", "ultrafast")
        cmd = [
            "ffmpeg", "-y",
            "-f", "v4l2",
            "-video_size", resolution,
            "-framerate", fps,
            "-i", device,
            "-c:v", codec,
            "-preset", preset,
            # fragmented MP4: file stays playable even if ffmpeg dies mid-recording
            "-movflags", "+frag_keyframe+empty_moov",
            str(_current_file),
        ]
        log.info("ffmpeg cmd: %s", " ".join(cmd))
        # stderr must not be PIPE: unread pipe fills up and freezes ffmpeg
        _recording_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=_FFMPEG_LOG,
        )

    # camera setup takes ~0.3-0.5s after the .wav appeared — remember the gap
    # so the mux can trim it off the audio, otherwise sound lags the image
    global _av_offset
    if _wav_detected_at is not None:
        _av_offset = min(max(time.monotonic() - _wav_detected_at, 0.0), 5.0)
    else:
        _av_offset = 0.0

    # safety net: handset left off the hook must not fill the SD card
    limit = int(cfg.get("recording_limit", 300)) + 10  # margin over upstream audio limit
    _limit_timer = threading.Timer(limit, _on_limit_reached, args=(cfg, limit))
    _limit_timer.daemon = True
    _limit_timer.start()


def _on_limit_reached(cfg: dict, limit: int):
    log.warning("Recording hit %ds limit — stopping video (handset off the hook?)", limit)
    stop_recording(cfg)


def stop_recording(cfg: dict):
    with _lock:
        _stop_recording_locked(cfg)


def _stop_recording_locked(cfg: dict):
    global _recording_proc, _picamera, _recording_start, _current_file, _last_stop_time, _limit_timer

    if _recording_proc is None and _picamera is None and _current_file is None:
        log.warning("stop_recording called but nothing was recording — ignoring")
        return

    if _limit_timer is not None:
        _limit_timer.cancel()
        _limit_timer = None

    set_status("saving")
    duration = time.monotonic() - (_recording_start or 0)
    min_dur = cfg.get("video", {}).get("min_duration_seconds", 2)

    if _picamera is not None:
        try:
            _picamera.stop_recording()
            _picamera.close()
        except Exception as e:
            log.error("picamera2 stop error: %s", e)
        finally:
            _picamera = None

    if _recording_proc is not None:
        if _recording_proc.poll() is not None:
            log.error(
                "ffmpeg had already exited (code %s) — camera missing or capture failed; see ffmpeg.log",
                _recording_proc.returncode,
            )
        try:
            _recording_proc.terminate()
            _recording_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _recording_proc.kill()
            _recording_proc.wait()
        except Exception as e:
            log.error("ffmpeg stop error: %s", e)
        finally:
            _recording_proc = None

    saved_file = _current_file

    if saved_file and not saved_file.exists():
        log.error("No video file was produced (%s) — nothing saved", saved_file)
        saved_file = None
    elif saved_file and duration < min_dur:
        log.info("Recording too short (%.1fs < %ds) — deleting %s", duration, min_dur, saved_file)
        try:
            saved_file.unlink(missing_ok=True)
        except Exception as e:
            log.error("Could not delete short recording: %s", e)
        saved_file = None
    else:
        log.info("Recording saved: %s (%.1fs)", saved_file, duration)

    _current_file = None
    _recording_start = None
    _last_stop_time = time.monotonic()
    set_status("idle")

    if saved_file:
        # non-daemon so a shutdown mid-backup waits for the copy to finish
        threading.Thread(target=_post_process, args=(saved_file, _av_offset), daemon=False).start()


def _find_usb_mounts() -> list:
    """USB partitions ready to receive the backup, mounting them ourselves
    if needed — the headless image has no automounter, and we run as root."""
    try:
        lsblk = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,TYPE,TRAN,RM,MOUNTPOINT"],
            capture_output=True, text=True, timeout=10,
        )
        info = json.loads(lsblk.stdout)
    except Exception as e:
        log.error("lsblk failed: %s", e)
        return []

    mounts = []
    for disk in info.get("blockdevices", []):
        if disk.get("tran") != "usb":
            continue
        # partitions if the stick has them, the bare disk if not
        for part in disk.get("children") or [disk]:
            mp = part.get("mountpoint")
            if mp in ("/", "/boot", "/boot/firmware"):
                continue  # never treat the boot drive as a backup target
            if mp:
                mounts.append(Path(mp))
                continue
            target = Path("/media") / part["name"]
            try:
                target.mkdir(parents=True, exist_ok=True)
                r = subprocess.run(
                    ["mount", f"/dev/{part['name']}", str(target)],
                    capture_output=True, text=True, timeout=15,
                )
            except Exception as e:
                log.error("Mounting /dev/%s failed: %s", part["name"], e)
                continue
            if r.returncode == 0:
                log.info("Mounted /dev/%s → %s", part["name"], target)
                mounts.append(target)
            else:
                log.warning("Could not mount /dev/%s: %s", part["name"], r.stderr.strip())
    return mounts


def _generate_thumbnail(video_path: Path):
    thumb_path = video_path.with_suffix(".jpg")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", "1",
                "-i", str(video_path),
                "-vframes", "1",
                "-q:v", "2",
                str(thumb_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        if thumb_path.exists():
            log.info("Thumbnail saved: %s", thumb_path.name)
        else:
            log.warning("Thumbnail generation produced no output for %s", video_path.name)
    except Exception as e:
        log.error("Thumbnail failed: %s", e)


def _find_paired_wav(video_path: Path) -> Path | None:
    # upstream names wavs with its own timestamp, so stems never match —
    # pair by mtime proximity: calls are serialized, both files stop at the
    # same hang-up, so the wav that finished nearest our mp4 is the pair
    try:
        mp4_mtime = video_path.stat().st_mtime
        wavs = [w for w in video_path.parent.glob("*.wav")
                if abs(w.stat().st_mtime - mp4_mtime) < 30]
        return max(wavs, key=lambda w: w.stat().st_mtime) if wavs else None
    except OSError:
        return None


def _ffprobe_duration(path: Path, stream: str | None = None) -> float | None:
    cmd = ["ffprobe", "-v", "error"]
    if stream:
        cmd += ["-select_streams", stream, "-show_entries", "stream=duration"]
    else:
        cmd += ["-show_entries", "format=duration"]
    cmd += ["-of", "csv=p=0", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip().splitlines()
        return float(out[0]) if out else None
    except Exception:
        return None


def _mux_audio(video_path: Path, av_offset: float = 0.0):
    """Merge the paired .wav into the mp4 (video stream copied, audio → AAC).
    Runs post-hangup so it never competes with a live recording. The original
    .wav is kept — the web panel's audio player still uses it. av_offset
    trims the head of the audio: the camera starts that many seconds after
    arecord did, so without the trim sound lags the image and the video
    freezes at the end while the leftover audio plays out."""
    wav = _find_paired_wav(video_path)
    if wav is None:
        log.info("No paired .wav for %s — leaving video silent", video_path.name)
        return
    tmp = video_path.with_suffix(".mux.mp4")
    # both streams stop on the same hang-up, so their duration difference IS
    # the real start lag (hook poll + camera/encoder spin-up). Measuring the
    # files beats the process-timing estimate (av_offset), which misses the
    # encoder startup; av_offset stays as fallback if ffprobe fails.
    wav_dur = _ffprobe_duration(wav)
    vid_dur = _ffprobe_duration(video_path, "v")
    if wav_dur and vid_dur:
        av_offset = min(max(wav_dur - vid_dur, 0.0), 5.0)
    trim = ["-ss", f"{av_offset:.3f}"] if av_offset > 0.05 else []
    if trim:
        log.info("Muxing with %.3fs audio head trim (camera start lag)", av_offset)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), *trim, "-i", str(wav),
             "-map", "0:v:0", "-map", "1:a:0", "-shortest",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", str(tmp)],
            stdout=subprocess.DEVNULL, stderr=_FFMPEG_LOG, timeout=120,
        )
        if tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(video_path)
            log.info("Muxed %s into %s", wav.name, video_path.name)
        else:
            raise RuntimeError("ffmpeg produced no output")
    except Exception as e:
        log.error("Mux failed (%s) — keeping separate tracks", e)
        tmp.unlink(missing_ok=True)


def _copy_to_usb(video_path: Path):
    mounts = _find_usb_mounts()
    if not mounts:
        log.info("No USB drives mounted — skipping backup")
        return

    wav = _find_paired_wav(video_path)
    candidates = [video_path, video_path.with_suffix(".jpg")] + ([wav] if wav else [])
    files = [f for f in candidates if f.exists()]

    for mount in mounts:
        dest_dir = mount / "time-capsule-cam"
        try:
            dest_dir.mkdir(exist_ok=True)
        except Exception as e:
            log.error("Cannot create backup dir on %s: %s", mount, e)
            continue
        for f in files:
            try:
                shutil.copy2(f, dest_dir / f.name)
                log.info("USB backup: %s → %s", f.name, mount.name)
            except Exception as e:
                log.error("USB copy failed (%s): %s", f.name, e)

    # flush to the stick now, so yanking it without unmounting loses nothing
    os.sync()


# measured on the AB13X dongle 2026-08-16: mains hum at 150/250/450 Hz that
# enters after the gain stage, so it must be filtered out, not gained out.
# Voice starts ~300 Hz, so the double highpass (24 dB/oct at 200) is safe.
# The dongle's internal AGC clips the ADC on shouting no matter the mixer
# gain: adeclip reconstructs the flat-topped peaks (in float) and the
# trailing limiter keeps the rebuilt peaks inside 16-bit on the way out.
_HUM_FILTER = ("adeclip,highpass=f=200,highpass=f=200,"
               "equalizer=f=250:t=h:w=60:g=-14,equalizer=f=450:t=h:w=60:g=-12,"
               "alimiter=limit=0.92:level=false")


def _filter_wav(video_path: Path):
    """Strip mains hum from the paired .wav in place, so both the web panel's
    audio player and the later mux get the clean track."""
    wav = _find_paired_wav(video_path)
    if wav is None:
        return
    tmp = wav.with_name(wav.name + ".tmp")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav), "-af", _HUM_FILTER,
             "-c:a", "pcm_s16le", "-f", "wav", str(tmp)],
            stdout=subprocess.DEVNULL, stderr=_FFMPEG_LOG, timeout=120,
        )
        if tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(wav)
            log.info("Hum-filtered %s", wav.name)
        else:
            raise RuntimeError("ffmpeg produced no output")
    except Exception as e:
        log.error("Hum filter failed (%s) — keeping raw audio", e)
        tmp.unlink(missing_ok=True)


def _post_process(video_path: Path, av_offset: float = 0.0):
    time.sleep(3)  # let upstream's arecord finish flushing the .wav
    _filter_wav(video_path)
    _mux_audio(video_path, av_offset)
    _generate_thumbnail(video_path)
    _copy_to_usb(video_path)


def _is_off_hook(gpio_val: int, hook_type: str, invert: bool = False) -> bool:
    # Same convention as upstream audioGuestBook.is_on_hook:
    # NC: HIGH = on-hook (handset down), LOW = off-hook
    # NO: LOW  = on-hook, HIGH = off-hook
    if hook_type.upper() == "NC":
        off = gpio_val == GPIO.LOW
    else:
        off = gpio_val == GPIO.HIGH
    return not off if invert else off


def _wait_for_wav(cfg: dict, channel: int, hook_type: str, invert: bool) -> bool:
    """Block until upstream's arecord creates its .wav (i.e. after greeting+beep),
    so video starts at the same moment as audio. Returns False if the guest
    hangs up while waiting. On timeout starts anyway — a broken mic must not
    also cost us the video."""
    recordings_dir = Path(cfg.get("recordings_path", str(RECORDINGS_DIR_FALLBACK)))
    before = set(recordings_dir.glob("*.wav"))
    # ponytail: 30s hardcoded — greeting+beep is ~11s; make it a config knob
    # if someone records a longer greeting
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        # blink the LED while the greeting/beep plays; caller sets it solid
        # once recording actually starts
        set_led(int(time.monotonic() * 2.5) % 2 == 0)
        if not _is_off_hook(GPIO.input(channel), hook_type, invert):
            set_led(False)
            return False
        if set(recordings_dir.glob("*.wav")) - before:
            global _wav_detected_at
            _wav_detected_at = time.monotonic()
            return True
        time.sleep(0.1)
    log.warning("No new .wav within 30s — is audio broken? Starting video anyway")
    _wav_detected_at = None
    return True


def _say(cfg: dict, name: str):
    """Spoken prompt through the handset. Best effort — the LED shows the
    same states, so wifi setup still works with a dead earpiece."""
    try:
        subprocess.run(
            ["aplay", "-q", "-D", cfg.get("alsa_hw_mapping", "default"), str(SOUNDS_DIR / f"{name}.wav")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        )
    except Exception as e:
        log.warning("Prompt %s failed: %s", name, e)


def _wifi_setup(cfg: dict, hook_pin: int):
    """Plugged in with the handset lifted: read a wifi QR code with the camera
    and join that network. Nothing is recorded meanwhile — upstream ignores a
    handset that was already up when it started, and we never reach the hook
    callback. Ends on hang-up, on a successful join or on timeout.
    LED: flickering = looking for a code, solid = connecting."""
    try:
        from pyzbar.pyzbar import decode, ZBarSymbol
    except ImportError:
        log.error("pyzbar missing (apt install python3-pyzbar) — wifi setup unavailable")
        return
    if not PICAMERA2_AVAILABLE:
        log.error("wifi setup needs the picamera backend")
        return

    hook_type = cfg.get("hook_type", "NC")
    invert = bool(cfg.get("invert_hook", False))
    log.info("Handset up at power-on — wifi setup mode")
    set_status("config")
    cam = None
    tried = joined = False
    try:
        _say(cfg, "wifi_start")
        cam = Picamera2()
        # full field of view at half the sensor resolution: a code on a phone
        # screen at arm's length is too few pixels at the 720p we record in
        width, height = 2304, 1296
        cam.configure(cam.create_video_configuration(main={"size": (width, height), "format": "YUV420"}))
        if "AfMode" in cam.camera_controls:
            cam.set_controls({"AfMode": 2})  # continuous autofocus
        cam.start()
        deadline = time.monotonic() + WIFI_SETUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not _is_off_hook(GPIO.input(hook_pin), hook_type, invert):
                log.info("Hung up — leaving wifi setup")
                return
            set_led(int(time.monotonic() * 5) % 2 == 0)
            # YUV420: the first `height` rows are the luma plane, i.e. grayscale
            gray = cam.capture_array("main")[:height, :width]
            for code in decode(gray, symbols=[ZBarSymbol.QRCODE]):
                net = wifi_qr.parse(code.data.decode("utf-8", "replace"))
                if net is None:
                    continue
                log.info("Read wifi code for %r", net["S"])
                set_led(True)
                _say(cfg, "wifi_connecting")
                tried = True
                joined = wifi_qr.join(net["S"], net.get("P", ""), net.get("H", "").lower() == "true")
                if joined:
                    _say(cfg, "wifi_ok")
                    return
                # same code still in view → retried on the next frame
                _say(cfg, "wifi_fail")
                break
        log.info("wifi setup timed out")
        _say(cfg, "wifi_timeout")
    except Exception as e:
        log.error("wifi setup failed: %s", e)
    finally:
        if cam is not None:
            try:
                cam.stop()
                cam.close()
            except Exception as e:
                log.error("picamera2 close error: %s", e)
        if tried and not joined:
            wifi_qr.restore()
        set_led(False)
        set_status("idle")


def make_hook_callback(cfg: dict):
    hook_type = cfg.get("hook_type", "NC")
    invert = bool(cfg.get("invert_hook", False))

    def callback(channel):
        val = GPIO.input(channel)
        if _is_off_hook(val, hook_type, invert):
            cooldown = cfg.get("video", {}).get("min_gap_seconds", 1.0)
            remaining = cooldown - (time.monotonic() - _last_stop_time)
            if remaining > 0:
                # lifted right after the previous hang-up: wait the gap out
                # instead of dropping the event — the pin won't change again,
                # so dropping would lose this whole session's video
                log.info("Off-hook during cooldown — starting in %.1fs", remaining)
                time.sleep(remaining)
                if not _is_off_hook(GPIO.input(channel), hook_type, invert):
                    log.info("Hung up during cooldown wait — nothing to record")
                    return
            log.info("Off-hook detected — waiting for audio .wav (greeting+beep)")
            if not _wait_for_wav(cfg, channel, hook_type, invert):
                log.info("Hung up before the beep — nothing to record")
                return
            set_status("recording")
            set_led(True)
            try:
                start_recording(cfg)
            except Exception as e:
                log.error("start_recording failed: %s", e)
                set_led(False)
                set_status("idle")
        else:
            log.info("On-hook detected")
            try:
                stop_recording(cfg)
            except Exception as e:
                log.error("stop_recording failed: %s", e)
                set_status("idle")
            finally:
                set_led(False)

    return callback


def setup_gpio(cfg: dict) -> int:
    global _led_pin
    hook_pin = cfg.get("hook_gpio", 22)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(hook_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    led = cfg.get("video", {}).get("led_gpio", 0)
    if led:
        _led_pin = led
        GPIO.setup(_led_pin, GPIO.OUT, initial=GPIO.LOW)
        log.info("GPIO %d configured as recording LED", _led_pin)

    return hook_pin


def poll_hook(cfg: dict, hook_pin: int):
    # ponytail: 50 ms polling instead of edge detection — trixie's kernel
    # dropped the sysfs interface RPi.GPIO events need, and upstream holds
    # the pin via lgpio so we can't claim it either. Register reads through
    # /dev/gpiomem claim nothing and coexist with upstream.
    callback = make_hook_callback(cfg)
    bounce = float(cfg.get("hook_bounce_time") or 0.1)
    stable = GPIO.input(hook_pin)
    changed_at = None
    log.info("GPIO %d polling for hook changes (debounce %.2fs)", hook_pin, bounce)

    if _is_off_hook(stable, cfg.get("hook_type", "NC"), bool(cfg.get("invert_hook", False))):
        if time.monotonic() < BOOT_WINDOW_SECONDS:
            _wifi_setup(cfg, hook_pin)
            # whatever the handset does next is an ordinary hook change
            stable = GPIO.input(hook_pin)
        else:
            # handset already lifted when we start (e.g. service restarted
            # mid-call): record now instead of waiting for the next lift
            log.info("Handset already off the hook at startup — starting recording")
            callback(hook_pin)
    cfg_mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0
    while True:
        # the web panel edits config.yaml (e.g. invert_hook) and only restarts
        # the audio service — re-read here so both processes stay in sync
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            mtime = cfg_mtime
        if mtime != cfg_mtime:
            cfg_mtime = mtime
            cfg = load_config()
            callback = make_hook_callback(cfg)
            bounce = float(cfg.get("hook_bounce_time") or 0.1)
            log.info("config.yaml changed — reloaded (invert_hook=%s)", cfg.get("invert_hook"))
        # ffmpeg crash detection: without this, status stays "recording"
        # with nothing capturing until the guest hangs up
        if _recording_proc is not None and _recording_proc.poll() is not None:
            log.error("ffmpeg exited unexpectedly (code %s) — resetting", _recording_proc.returncode)
            stop_recording(cfg)
            set_led(False)

        val = GPIO.input(hook_pin)
        if val == stable:
            changed_at = None
        else:
            now = time.monotonic()
            if changed_at is None:
                changed_at = now
            elif now - changed_at >= bounce:
                stable = val
                changed_at = None
                callback(hook_pin)
        time.sleep(0.05)


def shutdown(signum, frame):
    log.info("Shutting down…")
    if _recording_proc or _picamera:
        cfg = load_config()
        stop_recording(cfg)
    set_led(False)
    if GPIO_AVAILABLE:
        GPIO.cleanup()
    set_status("idle")
    sys.exit(0)


def main():
    cfg = load_config()

    if not cfg.get("video", {}).get("enabled", True):
        log.info("Video recording disabled in config — exiting")
        return

    set_status("idle")
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if GPIO_AVAILABLE:
        hook_pin = setup_gpio(cfg)
        log.info("video_recorder running — waiting for hook events")
        poll_hook(cfg, hook_pin)
    else:
        log.warning("GPIO unavailable — idle loop (dev mode)")
        while True:
            time.sleep(60)


if __name__ == "__main__":
    main()
