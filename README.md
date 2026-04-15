# blinkvault

Local motion capture and livestreaming for Blink cameras — no subscription required.

blinkvault keeps a continuous live stream from your Blink camera in memory, detects motion locally using frame differencing, and saves MP4 clips that include a configurable amount of footage **before and after** the motion event. It uses [blinkpy](https://github.com/fronzbot/blinkpy) by Kevin Fronczak only to authenticate and open the livestream — all motion detection, buffering, and clip storage happen on your own machine. No Blink subscription required, no cloud clip storage, no always-on disk writes.

---

## Why this is different

Every other Blink integration works the same way: poll Blink's cloud API every 30 seconds, wait for a new clip to appear, download it. This means you need a paid Blink subscription, you get ~30 second delays, and you only see footage *after* motion triggers — never before.

blinkvault takes a different approach:

| | blinkvault | blinkbridge / HA integration |
|---|---|---|
| Blink subscription required | **No** | Yes (for clip history) |
| Motion detection | **Local, frame-by-frame** | Cloud polling |
| Pre-roll footage | **Configurable (default 30s)** | None |
| Latency | **Real-time** | 30+ second delay |
| Disk I/O at rest | **None (RAM buffer)** | Constant segment writes |
| Setup complexity | **Single Python script** | Docker / Home Assistant |

---

## How it works

### Live stream via the IMMI protocol

Blink cameras do not expose RTSP. blinkvault uses [blinkpy](https://github.com/fronzbot/blinkpy)'s `BlinkLiveStream` class to speak Blink's proprietary **IMMI protocol** — a TLS-wrapped binary protocol that delivers a real MPEG-TS stream. We patch two bugs in blinkpy's implementation (partial reads in `recv()` and over-eager poll termination) to keep the stream stable.

### In-memory rolling buffer

Raw MPEG-TS data is fed directly into a Python `collections.deque` — a rolling timestamped window of byte chunks sized to your configured pre-roll. Nothing is written to disk while the camera is idle.

### Local motion detection

An ffmpeg process decodes the stream at **2 fps** and downscales to **320×180 grayscale**. Python computes the mean absolute pixel difference between consecutive frames using numpy. When the difference exceeds a configurable threshold, motion is declared. No cloud, no ML model, no subscription.

### Pre-roll clips

When motion fires, the in-memory buffer already contains your configured pre-roll window of footage. blinkvault slices the relevant byte range, writes a single temporary `.ts` file, and converts it to a clean MP4 with `ffmpeg`. The resulting clip starts *before* the motion event — you see the person walking up to the door, not just the moment they arrived.

### Auto-reconnect

Blink sessions time out after approximately 5–6 minutes. blinkvault detects stream termination and reconnects automatically with exponential backoff, maintaining continuous monitoring.

---

## Requirements

- Python 3.11+
- [ffmpeg](https://ffmpeg.org/) (must be on `$PATH`)
- A Blink account with a compatible camera (tested on Blink Video Doorbell)

---

## Installation

**From PyPI:**
```bash
pip install blinkvault
```

**From source:**
```bash
git clone https://github.com/karl-dykema/blinkvault
cd blinkvault
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

> **Requires [ffmpeg](https://ffmpeg.org/)** on your `$PATH`.
> macOS: `brew install ffmpeg`

---

## Web interface

```bash
blinkvault
```

Open **http://localhost:8080** in your browser.

On first run you will be prompted for your Blink email, password, and a two-factor authentication code. Credentials are saved to `creds.json` (gitignored) and reused on subsequent runs.

### Features

- **Initiate Stream Connection / End Connection** — start and stop the monitoring daemon
- **Live snapshot** — refreshes every 2 seconds so you can see what the camera sees
- **● REC indicator** — pulses red in the header whenever a clip is being saved
- **Record Now** — grab a clip on demand without waiting for motion
- **Pre-roll** — configurable seconds of footage to include *before* the motion trigger
- **Post-roll** — configurable seconds of footage to include *after* the motion trigger
- **Motion sensitivity** — tune the frame-diff threshold (lower = more sensitive)
- **Cooldown** — minimum seconds between consecutive motion triggers
- **Clip browser** — collapsible list with formatted timestamps, file sizes, inline playback and download
- **Activity log** — live feed of stream events, motion triggers, and clip saves

---

## CLI livestream

```bash
# Watch live in a player
blinkvault-stream --output - | ffplay -

# Record to file
blinkvault-stream --output recording.mp4

# Stream to UDP (e.g. for VLC or Frigate)
blinkvault-stream --output udp://127.0.0.1:1234

# Pick a specific camera by name
blinkvault-stream --camera "Front Door" --output recording.mp4
```

---

## Configuration

Settings are saved to `capture_config.json` (gitignored) via the web UI, or you can edit the file directly:

```json
{
  "pre_roll": 30,
  "clip_duration": 30,
  "camera_name": "",
  "motion_threshold": 10,
  "cooldown": 60
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `pre_roll` | `30` | Seconds of footage before the motion trigger to include |
| `clip_duration` | `30` | Seconds of footage after the motion trigger to include |
| `camera_name` | `""` | Camera name as shown in the Blink app. Leave blank to use the first camera found. |
| `motion_threshold` | `10` | Mean pixel difference to declare motion (1–50). Lower is more sensitive. |
| `cooldown` | `60` | Minimum seconds between motion triggers |

### Debug log

blinkvault writes a rotating log to `blinkvault.log` in your working directory (max ~1.5 MB across 3 files). It records stream connect/disconnect events, session durations, motion triggers, clip saves, and errors.

---

## Privacy note

`creds.json` contains your Blink access token. It is gitignored and never leaves your machine. Clips, config, and logs are also gitignored.

---

## Acknowledgements

- **[blinkpy](https://github.com/fronzbot/blinkpy)** by Kevin Fronczak — Python API library for Blink cameras. blinkvault is built on top of blinkpy for authentication, camera discovery, and the `BlinkLiveStream` IMMI protocol implementation.
- **[FFmpeg](https://ffmpeg.org/)** — used for stream demuxing, grayscale frame extraction, motion analysis, and MP4 encoding.
- **[FastAPI](https://fastapi.tiangolo.com/)** — web framework powering the local UI.
- **[numpy](https://numpy.org/)** — frame differencing for local motion detection.

---

## License

GPL-3.0 — see [LICENSE](LICENSE)
