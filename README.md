# time-capsule-cam

Video sidecar for [rotary-phone-audio-guestbook](https://github.com/nickpourazima/rotary-phone-audio-guestbook).  
Adds synchronized video recording: when a guest lifts the handset, audio **and** video start together.

---

## Requirements

Montaje físico completo (teléfono, cableados, cámara): **[docs/hardware.md](docs/hardware.md)**.

- Raspberry Pi 4 — 1 GB RAM is enough (the installer raises swap to 1 GB as an OOM guard; 2 GB+ if you want headroom)
- Upstream image flashed and running: [download here](https://github.com/nickpourazima/rotary-phone-audio-guestbook/releases)
- USB webcam or Raspberry Pi Camera Module

---

## Install

SSH into the Pi and run:

```bash
curl -fsSL https://raw.githubusercontent.com/fiblayza/time-capsule-cam/main/install.sh | bash
```

That's it. The script:

1. Clones this repo to `/home/admin/time-capsule-cam/`
2. Installs `ffmpeg` and Python dependencies
3. Adds the `video:` section to the upstream `config.yaml`
4. Patches the upstream webserver to expose `/api/status`
5. Enables and starts the `video_recorder` systemd service

To update to a newer version, just run the same command again — it's idempotent.

---

## Configuration

After installing, edit `/home/admin/rotary-phone-audio-guestbook/config.yaml`:

```yaml
video:
  enabled: true
  backend: usb           # "usb" or "picamera"
  device: /dev/video0
  resolution: "1280x720"
  fps: 25
  min_duration_seconds: 2
  codec: libx264
  preset: ultrafast
  led_gpio: 17           # BCM pin for recording LED; 0 to disable
```

Two safety behaviours are always on:

- Video is written as **fragmented MP4**, so the file stays playable even if ffmpeg dies mid-recording (power cut, crash).
- If the handset is left off the hook, video stops automatically at the upstream `recording_limit` (default 300 s) + 10 s, instead of filling the SD card.

For the `picamera` backend install the library via apt, **not pip**: `sudo apt install python3-picamera2`.

Then restart the sidecar:

```bash
sudo systemctl restart video_recorder.service
```

---

## Verify

```bash
systemctl status video_recorder.service   # should be active (running)
cat /home/admin/time-capsule-cam/status.json  # {"status": "idle"}
```

Lift the handset → status becomes `recording`, an `.mp4` appears in `/recordings/`.  
Hang up → status goes `saving` → `idle`, file is complete.

The web panel at `http://<PI_IP>:8080` shows a live status badge (polls every 2 s), and each audio recording gets its paired video player right next to it (matched by timestamp — the video always starts before the audio, since audio only begins after the greeting).

ffmpeg output goes to `time-capsule-cam/ffmpeg.log` if you need to debug a capture problem.

> **⚠ Check on real hardware before the event:** upstream reads the hook pin with `gpiozero` (lgpio backend on Bookworm) and this sidecar uses `RPi.GPIO`. On a Pi 4 they coexist, but if `RPi.GPIO` resolves to the `rpi-lgpio` shim the second reader fails with "GPIO busy". Lift the handset with both services running and confirm both react.

---

## USB backup

If a USB drive is connected, each session (`.mp4` + `.wav`) is automatically copied to it after the handset is hung up. Works with any drive formatted as **exFAT** or FAT32 — no configuration needed. The sidecar mounts the drive itself at backup time (the headless image has no automounter), so hot-plugging mid-event works, and it syncs after every copy so pulling the stick without unmounting is safe.

Files are copied to a `time-capsule-cam/` folder on the drive. Multiple drives are supported; if none is connected the backup is silently skipped.

A 3-second delay is applied before copying to ensure the upstream audio process has finished writing the `.wav`.

---

## Thumbnails

A JPEG thumbnail (`.jpg`, same name as the recording) is extracted from the first second of each video automatically. The web panel can use it to preview sessions without loading the full video.

---

## Recording LED

An optional LED lights up while a guest is recording and turns off when they hang up.

**Wiring** (BCM pin 17 by default):

```
Pi GPIO 17 ──── 220Ω resistor ──── LED (+) ──── LED (−) ──── GND
```

Change the pin in `config.yaml` (`led_gpio: <pin>`), or set `led_gpio: 0` to disable it entirely.

---

## How it works

```
audioGuestBook.py  ──┐
                     ├── GPIO hook pin (both read independently)
video_recorder.py  ──┘
        │
        ├── off-hook → starts ffmpeg → saves .mp4 to /recordings/
        ├── on-hook  → stops ffmpeg → status: idle
        └── writes status.json (idle / recording / saving)

webserver/server.py (patched)
        └── /api/status  → reads status.json
```

Audio and video files share the same timestamp prefix:

```
recordings/
├── 2024-06-15_17-32-04.wav   ← upstream audio
└── 2024-06-15_17-32-04.mp4   ← our video
```
