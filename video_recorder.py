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

_last_stop_time = 0.0        # monotonic time of last stop, for cooldown
_recording_proc = None       # ffmpeg subprocess
_picamera = None             # picamera2 instance
_recording_start = None      # epoch float
_current_file = None         # Path of the file being written
_led_pin = None              # BCM pin for recording LED (None = disabled)
_limit_timer = None          # threading.Timer that stops runaway recordings
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
        threading.Thread(target=_post_process, args=(saved_file,), daemon=False).start()


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


def _copy_to_usb(video_path: Path):
    time.sleep(3)  # give upstream audioGuestBook time to finish writing the .wav

    mounts = _find_usb_mounts()
    if not mounts:
        log.info("No USB drives mounted — skipping backup")
        return

    stem = video_path.stem
    recordings_dir = video_path.parent
    candidates = [
        video_path,
        video_path.with_suffix(".jpg"),
        recordings_dir / f"{stem}.wav",
    ]
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


def _post_process(video_path: Path):
    _generate_thumbnail(video_path)
    _copy_to_usb(video_path)


def _is_off_hook(gpio_val: int, hook_type: str, invert: bool = False) -> bool:
    # NC (normally closed): pin goes HIGH when handset is lifted
    # NO (normally open):   pin goes LOW  when handset is lifted
    if hook_type.upper() == "NC":
        off = gpio_val == GPIO.HIGH
    else:
        off = gpio_val == GPIO.LOW
    return not off if invert else off


def make_hook_callback(cfg: dict):
    hook_type = cfg.get("hook_type", "NC")
    invert = bool(cfg.get("invert_hook", False))

    def callback(channel):
        val = GPIO.input(channel)
        if _is_off_hook(val, hook_type, invert):
            cooldown = cfg.get("video", {}).get("min_gap_seconds", 1.0)
            if time.monotonic() - _last_stop_time < cooldown:
                log.warning("Off-hook ignored — cooldown active, wait %.1fs", cooldown)
                return
            log.info("Off-hook detected")
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

    # handset already lifted when we start (e.g. service restarted mid-call):
    # record now instead of waiting for the next hang-up/lift cycle
    if _is_off_hook(stable, cfg.get("hook_type", "NC"), bool(cfg.get("invert_hook", False))):
        log.info("Handset already off the hook at startup — starting recording")
        callback(hook_pin)
    while True:
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
