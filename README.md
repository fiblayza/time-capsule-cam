# time-capsule-cam

Video sidecar for [rotary-phone-audio-guestbook](https://github.com/nickpourazima/rotary-phone-audio-guestbook).  
Adds synchronized video recording: when a guest lifts the handset, audio **and** video start together.

---

## Requirements

- Raspberry Pi 4 (2 GB RAM min)
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

The web panel at `http://<PI_IP>:8080` shows a live status badge (polls every 2 s).

---

## USB backup

If a USB drive is connected, each session (`.mp4` + `.wav`) is automatically copied to it after the handset is hung up. Works with any drive formatted as **exFAT** or FAT32 — no configuration needed, the Pi auto-mounts it under `/media/admin/<LABEL>`.

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
