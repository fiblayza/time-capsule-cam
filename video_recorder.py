import subprocess
import json
import time
import yaml
import logging
import signal
import sys
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
    from picamera2.outputs import FileOutput
    PICAMERA2_AVAILABLE = True
except ImportError:
    PICAMERA2_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "rotary-phone-audio-guestbook" / "config.yaml"
RECORDINGS_DIR_FALLBACK = Path(__file__).parent.parent / "recordings"
STATUS_PATH = Path(__file__).parent / "status.json"
RECORDINGS_DIR = None  # resolved from config at runtime

DEBOUNCE_MS = 200
_last_event_time = 0.0
_recording_proc = None       # ffmpeg subprocess
_picamera = None             # picamera2 instance
_recording_start = None      # epoch float
_current_file = None         # Path of the file being written


def set_status(state: str):
    STATUS_PATH.write_text(json.dumps({"status": state}))
    log.info("Status → %s", state)


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
            "codec": "libx264",
            "preset": "ultrafast",
        },
    }


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def start_recording(cfg: dict):
    global _recording_proc, _picamera, _recording_start, _current_file

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
            return
        width, height = map(int, vcfg.get("resolution", "1280x720").split("x"))
        fps = int(vcfg.get("fps", 25))
        _picamera = Picamera2()
        video_config = _picamera.create_video_configuration(
            main={"size": (width, height)},
        )
        _picamera.configure(video_config)
        encoder = H264Encoder(bitrate=2_000_000)
        output = FileOutput(str(_current_file))
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
            str(_current_file),
        ]
        log.info("ffmpeg cmd: %s", " ".join(cmd))
        _recording_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )


def stop_recording(cfg: dict):
    global _recording_proc, _picamera, _recording_start, _current_file

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

    if _current_file and duration < min_dur:
        log.info("Recording too short (%.1fs < %ds) — deleting %s", duration, min_dur, _current_file)
        try:
            _current_file.unlink(missing_ok=True)
        except Exception as e:
            log.error("Could not delete short recording: %s", e)
    else:
        log.info("Recording saved: %s (%.1fs)", _current_file, duration)

    _current_file = None
    _recording_start = None
    set_status("idle")


def _is_off_hook(gpio_val: int, hook_type: str) -> bool:
    # NC (normally closed): pin goes HIGH when handset is lifted
    # NO (normally open):   pin goes LOW  when handset is lifted
    if hook_type.upper() == "NC":
        return gpio_val == GPIO.HIGH
    return gpio_val == GPIO.LOW


def make_hook_callback(cfg: dict):
    hook_type = cfg.get("hook_type", "NC")

    def callback(channel):
        global _last_event_time
        now = time.monotonic()
        if now - _last_event_time < DEBOUNCE_MS / 1000:
            return
        _last_event_time = now

        val = GPIO.input(channel)
        if _is_off_hook(val, hook_type):
            log.info("Off-hook detected")
            set_status("recording")
            try:
                start_recording(cfg)
            except Exception as e:
                log.error("start_recording failed: %s", e)
                set_status("idle")
        else:
            log.info("On-hook detected")
            try:
                stop_recording(cfg)
            except Exception as e:
                log.error("stop_recording failed: %s", e)
                set_status("idle")

    return callback


def setup_gpio(cfg: dict):
    gpio_pin = cfg.get("hook_gpio", 11)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    callback = make_hook_callback(cfg)
    GPIO.add_event_detect(gpio_pin, GPIO.BOTH, callback=callback, bouncetime=DEBOUNCE_MS)
    log.info("GPIO %d watching for hook events", gpio_pin)


def shutdown(signum, frame):
    log.info("Shutting down…")
    if _recording_proc or _picamera:
        cfg = load_config()
        stop_recording(cfg)
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
        setup_gpio(cfg)
        log.info("video_recorder running — waiting for hook events")
        signal.pause()
    else:
        log.warning("GPIO unavailable — idle loop (dev mode)")
        while True:
            time.sleep(60)


if __name__ == "__main__":
    main()
