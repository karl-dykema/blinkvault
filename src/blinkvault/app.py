"""
Blink Motion Capture — v1
Web UI + background daemon that records MP4 clips on motion events.

Run: python app.py
Then open: http://localhost:8080
"""

import asyncio
import collections
import json
import logging
import logging.handlers
import ssl
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import numpy as np

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from blinkpy import api
from blinkpy.auth import Auth, BlinkTwoFARequiredError, LoginError
from blinkpy.blinkpy import Blink
from blinkpy.livestream import BlinkLiveStream

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("blinkvault")
log.setLevel(logging.INFO)

BASE_DIR    = Path.cwd()
CREDS_FILE  = BASE_DIR / "creds.json"
CONFIG_FILE = BASE_DIR / "capture_config.json"
CLIPS_DIR   = BASE_DIR / "clips"
CLIPS_DIR.mkdir(exist_ok=True)

_file_handler = logging.handlers.RotatingFileHandler(
    BASE_DIR / "blinkvault.log", maxBytes=500_000, backupCount=2,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_file_handler)

PRE_ROLL_SECONDS = 30           # seconds of footage before motion to include

DEFAULT_CONFIG = {
    "pre_roll": 30,             # seconds of footage before motion to include
    "clip_duration": 30,
    "camera_name": "",          # blank = first camera found
    "motion_threshold": 10,     # mean pixel diff (0–255); lower = more sensitive
    "cooldown": 60,             # seconds between motion triggers
}

# Frame size for motion analysis — small = fast, lower CPU
ANALYSIS_W, ANALYSIS_H = 320, 180
ANALYSIS_FPS = 2


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_FILE.exists():
        return {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text())}
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# Resilient livestream (fixes blinkpy partial-read + poll-fragility bugs)
# ---------------------------------------------------------------------------

class ResilientLiveStream(BlinkLiveStream):
    def __init__(self, camera, response):
        super().__init__(camera, response)
        self.on_data = None  # optional callback(bytes) called for every raw MPEG-TS chunk

    async def recv(self):
        try:
            while not self.target_reader.at_eof():
                try:
                    header = await self.target_reader.readexactly(9)
                except asyncio.IncompleteReadError:
                    break
                msgtype = header[0]
                payload_length = int.from_bytes(header[5:9], byteorder="big")
                if payload_length <= 0:
                    continue
                try:
                    data = await self.target_reader.readexactly(payload_length)
                except asyncio.IncompleteReadError:
                    break
                if msgtype != 0x00 or data[0] != 0x47:
                    continue
                if self.on_data:
                    self.on_data(data)
                for writer in list(self.clients):
                    if not writer.is_closing():
                        writer.write(data)
                        await writer.drain()
                await asyncio.sleep(0)
        except ssl.SSLError as e:
            if e.reason != "APPLICATION_DATA_AFTER_CLOSE_NOTIFY":
                pass
        except Exception:
            pass
        finally:
            self.target_writer.close()

    async def poll(self):
        failures = 0
        try:
            while not self.target_reader.at_eof():
                await asyncio.sleep(self.polling_interval)
                try:
                    response = await api.request_command_status(
                        self.camera.sync.blink,
                        self.camera.network_id,
                        self.command_id,
                    )
                    failures = 0
                    for cmd in response.get("commands", []):
                        if cmd.get("id") == self.command_id:
                            if cmd.get("state_condition") not in ("new", "running"):
                                return
                except Exception:
                    failures += 1
                    if failures >= 5:
                        return
        finally:
            try:
                await api.request_command_done(
                    self.camera.sync.blink,
                    self.camera.network_id,
                    self.command_id,
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Blink auth
# ---------------------------------------------------------------------------

def load_creds() -> dict:
    if CREDS_FILE.exists():
        return json.loads(CREDS_FILE.read_text())
    return {}


def save_creds(auth: Auth) -> None:
    CREDS_FILE.write_text(json.dumps(auth.login_attributes, indent=2))


async def authenticate(blink: Blink) -> None:
    creds = load_creds()
    blink.auth = Auth(creds if creds else {"username": None, "password": None},
                      no_prompt=not creds)
    try:
        await blink.start()
        save_creds(blink.auth)
        return
    except BlinkTwoFARequiredError:
        pass
    except LoginError:
        log.warning("Saved credentials failed, re-authenticating")
        CREDS_FILE.unlink(missing_ok=True)
        blink.auth = Auth({"username": None, "password": None}, no_prompt=False)
        try:
            await blink.start()
            save_creds(blink.auth)
            return
        except BlinkTwoFARequiredError:
            pass

    code = input("Enter Blink two-factor authentication code: ").strip()
    if not await blink.send_2fa_code(code):
        sys.exit("Two-factor authentication failed.")
    save_creds(blink.auth)


def find_camera(blink: Blink, name: str):
    cameras = {}
    for sync in blink.sync.values():
        cameras.update(sync.cameras)
    if not cameras:
        return None, None
    if name:
        cam = cameras.get(name)
        return (name, cam) if cam else (None, None)
    cam_name, cam = next(iter(cameras.items()))
    return cam_name, cam


# ---------------------------------------------------------------------------
# Clip recording
# ---------------------------------------------------------------------------

async def init_livestream(camera) -> ResilientLiveStream:
    response = await api.request_camera_liveview(
        camera.sync.blink,
        camera.sync.network_id,
        camera.camera_id,
        camera_type=camera.camera_type,
    )
    if "server" not in response:
        raise RuntimeError(f"Liveview API returned no server URL: {response}")
    if not response["server"].startswith("immis://"):
        raise RuntimeError(f"Unsupported stream protocol: {response['server']}")
    return ResilientLiveStream(camera, response)


async def record_clip(camera, duration: int, out_path: Path) -> bool:
    """Record `duration` seconds of live video to out_path (MP4). Returns True on success."""
    try:
        ls = await init_livestream(camera)
        await ls.start(host="127.0.0.1", port=0)
        url = ls.url
        feed_task = asyncio.create_task(ls.feed())
        await asyncio.sleep(1.5)  # let the feed authenticate

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-y",
            "-analyzeduration", "10000000", "-probesize", "10000000",
            "-i", url,
            "-t", str(duration),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-ss", "4",               # output-side skip: drop warmup blank frames
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-f", "mp4",
            str(out_path),
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        feed_task.cancel()
        try:
            await feed_task
        except asyncio.CancelledError:
            pass
        ls.stop()

        return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0
    except Exception as e:
        log.error("record_clip error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Daemon state
# ---------------------------------------------------------------------------

class Daemon:
    def __init__(self):
        self.running = False
        self.recording = False
        self.last_event: str | None = None
        self.log: list[str] = []
        self._task: asyncio.Task | None = None
        self._blink: Blink | None = None
        self._ts_buf: collections.deque | None = None  # shared with _stream_and_detect
        self._proxy_url: str | None = None
        self._latest_jpeg: bytes | None = None

    def _emit(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        log.info(msg)
        self.log.insert(0, entry)
        if len(self.log) > 50:
            self.log.pop()

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._blink and self._blink.auth and hasattr(self._blink.auth, "session"):
            try:
                await self._blink.auth.session.close()
            except Exception:
                pass
        self._blink = None
        self._emit("Stream connection closed")

    async def _run(self) -> None:
        self._emit("Starting — authenticating with Blink...")
        try:
            self._blink = Blink(motion_interval=0, refresh_rate=30)
            await authenticate(self._blink)
        except Exception as e:
            self._emit(f"Auth failed: {e}")
            self.running = False
            return

        cfg = load_config()
        cam_name, camera = find_camera(self._blink, cfg.get("camera_name", ""))
        if camera is None:
            self._emit("No camera found. Check camera_name in config.")
            self.running = False
            return

        self._emit(f"Monitoring: {cam_name} — local motion detection active")

        try:
            consecutive_short = 0
            while self.running:
                stream_start = time.monotonic()
                try:
                    await self._stream_and_detect(camera, cam_name)
                    duration = time.monotonic() - stream_start
                    if duration < 15:
                        consecutive_short += 1
                        delay = min(10 * consecutive_short, 60)
                        self._emit(f"Stream ended quickly ({duration:.0f}s) — backing off {delay}s")
                        await asyncio.sleep(delay)
                    else:
                        consecutive_short = 0
                        self._emit(f"Stream session ended normally ({duration:.0f}s) — reconnecting")
                        await asyncio.sleep(3)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    consecutive_short += 1
                    delay = 30 if "busy" in str(e).lower() or "307" in str(e) else min(10 * consecutive_short, 60)
                    self._emit(f"Stream error ({type(e).__name__}): {e} — reconnecting in {delay}s")
                    await asyncio.sleep(delay)
                    # Re-fetch camera after reconnect
                    cfg = load_config()
                    _, camera = find_camera(self._blink, cfg.get("camera_name", "") or cam_name)
                    if camera is None:
                        self._emit("Camera lost, stopping.")
                        self.running = False
                        return

        except asyncio.CancelledError:
            self._emit("Stream stopped")
            raise
        except BaseException as e:
            self._emit(f"Daemon crashed: {type(e).__name__}: {e}")
            self.running = False
            raise

    async def _stream_and_detect(self, camera, cam_name: str) -> None:
        """
        Two ffmpeg clients on the proxy:
          1. Buffer reader  — pipes raw MPEG-TS into a rolling in-memory deque (~45 MB max)
          2. Analysis reader — pipes low-FPS grayscale frames for motion detection
        On motion, slice the buffer, write one temp .ts, convert to MP4. No constant disk I/O.
        """
        ls = await init_livestream(camera)
        await ls.start(host="127.0.0.1", port=0)
        proxy_url = ls.url
        feed_task = asyncio.create_task(ls.feed())
        self._proxy_url = proxy_url
        asyncio.create_task(self._snapshot_loop(proxy_url))
        await asyncio.sleep(2)
        self._emit("Stream open — monitoring for motion")

        # Rolling in-memory buffer filled directly from recv() — no extra ffmpeg process needed
        ts_buf: collections.deque = collections.deque()
        self._ts_buf = ts_buf  # expose for Record Now
        _pre_roll = [load_config().get("pre_roll", DEFAULT_CONFIG["pre_roll"])]

        def _on_data(chunk: bytes) -> None:
            now = time.monotonic()
            ts_buf.append((now, chunk))
            if not self.recording:
                cutoff = now - _pre_roll[0] - 2
                while ts_buf and ts_buf[0][0] < cutoff:
                    ts_buf.popleft()

        ls.on_data = _on_data

        frame_size = ANALYSIS_W * ANALYSIS_H
        analysis_proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error",
            "-fflags", "+nobuffer+discardcorrupt",
            "-analyzeduration", "2000000",
            "-probesize", "1000000",
            "-i", proxy_url,
            "-vf", f"scale={ANALYSIS_W}:{ANALYSIS_H},fps={ANALYSIS_FPS}",
            "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        prev_frame: np.ndarray | None = None
        last_trigger = 0.0
        frame_count = 0

        try:
            while self.running:
                cfg = load_config()
                _pre_roll[0] = cfg.get("pre_roll", DEFAULT_CONFIG["pre_roll"])
                data = await analysis_proc.stdout.readexactly(frame_size)
                frame = np.frombuffer(data, dtype=np.uint8)
                frame_count += 1

                if prev_frame is not None and frame_count > ANALYSIS_FPS * 2:
                    diff = float(np.mean(np.abs(frame.astype(np.int16) - prev_frame.astype(np.int16))))
                    now = time.monotonic()
                    if (
                        diff > cfg.get("motion_threshold", 10)
                        and (now - last_trigger) > cfg.get("cooldown", 60)
                        and not self.recording
                    ):
                        last_trigger = now
                        self.recording = True
                        motion_ts = now
                        pre_roll = _pre_roll[0]
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        out_path = CLIPS_DIR / f"motion_{ts}.mp4"
                        self._emit(f"Motion! diff={diff:.1f} — saving {pre_roll}s pre + {cfg['clip_duration']}s post: {out_path.name}")
                        self.last_event = ts
                        asyncio.create_task(
                            self._save_clip_from_buffer(ts_buf, motion_ts, pre_roll, cfg["clip_duration"], out_path)
                        )

                prev_frame = frame

        except asyncio.IncompleteReadError:
            self._emit("Stream ended — will reconnect")
        finally:
            ls.on_data = None
            self._ts_buf = None
            self._proxy_url = None
            self._latest_jpeg = None
            try:
                analysis_proc.kill()
                await analysis_proc.wait()
            except Exception:
                pass
            feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass
            ls.stop()

    async def _snapshot_loop(self, proxy_url: str) -> None:
        while self._proxy_url == proxy_url:
            try:
                if self._ts_buf:
                    now = time.monotonic()
                    # Use a 10s window to ensure at least one keyframe is included
                    raw = b"".join(chunk for t, chunk in list(self._ts_buf) if t > now - 10)
                    if raw:
                        proc = await asyncio.create_subprocess_exec(
                            "ffmpeg", "-loglevel", "quiet",
                            "-f", "mpegts", "-i", "pipe:0",
                            "-vframes", "1", "-f", "image2pipe", "-vcodec", "mjpeg",
                            "pipe:1",
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        stdout, _ = await asyncio.wait_for(
                            proc.communicate(input=raw), timeout=5.0
                        )
                        if stdout:
                            self._latest_jpeg = stdout
            except Exception as e:
                self._emit(f"Snapshot error: {e}")
            await asyncio.sleep(2)

    async def _save_clip_from_buffer(
        self, ts_buf: collections.deque, motion_ts: float, pre_roll: int, duration: int, out_path: Path
    ) -> None:
        """Slice the in-memory buffer, write a temp .ts, convert to MP4."""
        try:
            await asyncio.sleep(duration + 1)

            pre_start = motion_ts - pre_roll
            clip_end  = motion_ts + duration

            chunks = list(ts_buf)
            raw = b"".join(chunk for t, chunk in chunks if pre_start <= t <= clip_end)
            if not raw:
                self._emit(f"Buffer empty for {out_path.name} (buf={len(chunks)} chunks, span={chunks[0][0]:.1f}–{chunks[-1][0]:.1f} want {pre_start:.1f}–{clip_end:.1f})" if chunks else f"Buffer empty for {out_path.name} (no chunks)")
                return

            tmp = out_path.with_suffix(".tmp.ts")
            tmp.write_bytes(raw)

            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-loglevel", "error", "-y",
                "-fflags", "+genpts",
                "-i", str(tmp),
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                "-f", "mp4", str(out_path),
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            tmp.unlink(missing_ok=True)

            if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
                self._emit(f"Saved: {out_path.name} ({out_path.stat().st_size//1024} KB, buf={len(raw)//1024} KB raw)")
            else:
                self._emit(f"Failed: {out_path.name}")
        except Exception as e:
            self._emit(f"Save error: {e}")
        finally:
            self.recording = False


daemon = Daemon()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Re-apply after uvicorn reconfigures logging at startup
    logging.getLogger("aiohttp.client").setLevel(logging.CRITICAL)
    logging.getLogger("blinkpy").setLevel(logging.CRITICAL)
    yield
    await daemon.stop()

app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(HTML)


@app.post("/daemon/start")
async def daemon_start():
    await daemon.start()
    return {"ok": True}


@app.post("/daemon/stop")
async def daemon_stop():
    await daemon.stop()
    return {"ok": True}


@app.post("/daemon/record")
async def daemon_record_now():
    """Trigger an immediate recording from the live buffer."""
    if not daemon.running:
        return {"ok": False, "error": "Daemon not running"}
    if daemon.recording:
        return {"ok": False, "error": "Already recording"}
    if daemon._ts_buf is None:
        return {"ok": False, "error": "Stream not ready yet — wait a moment and try again"}

    cfg = load_config()
    daemon.recording = True
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = CLIPS_DIR / f"manual_{ts}.mp4"
    pre_roll = cfg.get("pre_roll", DEFAULT_CONFIG["pre_roll"])
    daemon._emit(f"Manual record — {pre_roll}s pre + {cfg['clip_duration']}s post: {out_path.name}")
    asyncio.create_task(
        daemon._save_clip_from_buffer(daemon._ts_buf, time.monotonic(), pre_roll, cfg["clip_duration"], out_path)
    )
    return {"ok": True}


@app.get("/status")
async def status():
    cfg = load_config()
    clips = sorted(CLIPS_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
    clip_data = [
        {"name": f.name, "size": f.stat().st_size}
        for f in clips[:30]
    ]
    return {
        "running": daemon.running,
        "recording": daemon.recording,
        "last_event": daemon.last_event,
        "log": daemon.log[:50],
        "clips": clip_data,
        "config": cfg,
    }


@app.get("/config")
async def get_config():
    return load_config()


@app.post("/config")
async def set_config(request: Request):
    data = await request.json()
    cfg = load_config()
    for key in ("pre_roll", "clip_duration", "camera_name", "motion_threshold", "cooldown"):
        if key in data:
            cfg[key] = data[key]
    save_config(cfg)
    return {"ok": True, "config": cfg}


@app.get("/debug")
async def debug():
    if daemon._blink is None:
        return {"error": "daemon not running"}
    cameras = {}
    for sync_name, sync in daemon._blink.sync.items():
        for cam_name, cam in sync.cameras.items():
            cameras[cam_name] = {
                "last_record": cam.last_record,
                "motion_detected": cam.motion_detected,
                "recent_clips": getattr(cam, "recent_clips", []),
                "arm": getattr(sync, "arm", None),
                "last_records_raw": sync.last_records.get(cam_name, []),
                "motion_raw": sync.motion.get(cam_name, None),
            }
    return {"cameras": cameras, "last_refresh": daemon._blink.last_refresh}


@app.get("/clips/{filename}")
async def serve_clip(filename: str, request: Request):
    path = CLIPS_DIR / filename
    if not path.exists() or path.suffix != ".mp4":
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(
        str(path),
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes"},
    )


@app.delete("/clips/{filename}")
async def delete_clip(filename: str):
    path = CLIPS_DIR / filename
    if path.exists() and path.suffix == ".mp4":
        path.unlink()
    return {"ok": True}


@app.get("/snapshot.jpg")
async def snapshot():
    if daemon._latest_jpeg is None:
        raise HTTPException(status_code=503, detail="No snapshot available yet")
    return Response(
        content=daemon._latest_jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Inline HTML
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>blinkvault</title>
<script data-goatcounter="https://blinkvault.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #0f0f0f; color: #e0e0e0; }
  header { display: flex; align-items: center; gap: 12px; padding: 16px 24px;
           background: #1a1a1a; border-bottom: 1px solid #2a2a2a; }
  header h1 { font-size: 1.1rem; font-weight: 600; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #444; flex-shrink: 0; }
  .dot.on  { background: #22c55e; box-shadow: 0 0 6px #22c55e; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .status-label { font-size: .8rem; color: #888; }
  .rec-badge { font-size: .75rem; font-weight: 700; letter-spacing: .08em;
               color: #555; background: #2a2a2a; border: 1px solid #3a3a3a;
               border-radius: 4px; padding: 2px 7px; }
  .rec-badge.active { color: #ef4444; border-color: #ef4444;
                      box-shadow: 0 0 6px #ef444466; animation: pulse 1s infinite; }
  .btn { padding: 7px 16px; border: none; border-radius: 6px; cursor: pointer;
         font-size: .85rem; font-weight: 500; }
  .btn-green { background: #16a34a; color: #fff; }
  .btn-green:hover { background: #15803d; }
  .btn-red   { background: #dc2626; color: #fff; }
  .btn-red:hover   { background: #b91c1c; }
  .btn-gray  { background: #374151; color: #d1d5db; }
  .btn-gray:hover  { background: #4b5563; }
  main { display: grid; grid-template-columns: 340px 1fr; gap: 0; height: calc(100vh - 57px); }
  .sidebar { background: #141414; border-right: 1px solid #2a2a2a;
             display: flex; flex-direction: column; overflow: hidden; }
  .panel { padding: 16px; }
  .panel + .panel { border-top: 1px solid #2a2a2a; }
  .panel h2 { font-size: .75rem; font-weight: 600; text-transform: uppercase;
              letter-spacing: .08em; color: #6b7280; margin-bottom: 12px; }
  .log-box { font-size: .75rem; color: #9ca3af; line-height: 1.6;
             overflow-y: auto; flex: 1; padding: 12px 16px; }
  .log-box p { margin-bottom: 2px; }
  label { display: block; font-size: .8rem; color: #9ca3af; margin-bottom: 4px; }
  input[type=number], input[type=text] {
    width: 100%; padding: 6px 10px; border-radius: 6px;
    background: #1f2937; border: 1px solid #374151; color: #e0e0e0;
    font-size: .85rem; margin-bottom: 12px; }
  .content { overflow-y: auto; padding: 20px; }
  .clip-list { display: flex; flex-direction: column; gap: 6px; }
  .clip-row { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; overflow: hidden; }
  .clip-header { display: flex; align-items: center; gap: 12px; padding: 10px 14px; cursor: pointer;
                 user-select: none; }
  .clip-header:hover { background: #222; }
  .clip-icon { font-size: .9rem; flex-shrink: 0; }
  .clip-meta { flex: 1; min-width: 0; }
  .clip-date { font-size: .88rem; color: #e0e0e0; font-weight: 500; }
  .clip-sub  { font-size: .75rem; color: #6b7280; margin-top: 1px; }
  .clip-body { display: none; border-top: 1px solid #2a2a2a; }
  .clip-body.open { display: block; }
  .clip-body video { width: 100%; display: block; background: #000; max-height: 320px; }
  .clip-actions { padding: 8px 12px; display: flex; gap: 8px; }
  .empty { color: #4b5563; font-size: .9rem; margin-top: 40px; text-align: center; }
  .live-section { margin-bottom: 20px; }
  .live-section h2 { font-size: .75rem; font-weight: 600; text-transform: uppercase;
                     letter-spacing: .08em; color: #6b7280; margin-bottom: 10px; }
  #live-img { width: 100%; max-width: 640px; border-radius: 8px; background: #111;
              display: block; border: 1px solid #2a2a2a; color: transparent; }
  #live-placeholder { width: 100%; max-width: 640px; border-radius: 8px; background: #111;
                      border: 1px solid #2a2a2a; padding: 40px; text-align: center;
                      color: #4b5563; font-size: .85rem; }
</style>
</head>
<body>
<header>
  <div class="dot" id="dot"></div>
  <h1>blinkvault</h1>
  <span class="rec-badge" id="rec-badge">● REC</span>
  <span class="status-label" id="status-label">Stopped</span>
  <div style="margin-left:auto; display:flex; gap:8px;">
    <button class="btn btn-green" onclick="daemonStart()">Initiate Stream Connection</button>
    <button class="btn btn-red"   onclick="daemonStop()">End Connection</button>
    <button class="btn btn-gray"  onclick="recordNow()">Record Now</button>
  </div>
</header>
<main>
  <div class="sidebar">
    <div class="panel">
      <h2>Config</h2>
      <label>Pre-roll — seconds before motion trigger</label>
      <input type="number" id="cfg-preroll" min="5" max="120" value="30">
      <label>Post-roll — seconds after motion trigger</label>
      <input type="number" id="cfg-duration" min="5" max="300" value="30">
      <label>Motion sensitivity (1–50, lower = more sensitive)</label>
      <input type="number" id="cfg-threshold" min="1" max="50" value="10">
      <label>Cooldown between clips (seconds)</label>
      <input type="number" id="cfg-cooldown" min="10" max="600" value="60">
      <label>Camera name (blank = first found)</label>
      <input type="text" id="cfg-camera" placeholder="e.g. Front Door">
      <button class="btn btn-gray" style="width:100%" onclick="saveConfig()">Save Config</button>
    </div>
    <div class="panel" style="flex-shrink:0">
      <h2>Activity</h2>
    </div>
    <div class="log-box" id="log-box"></div>
  </div>
  <div class="content">
    <div class="live-section" id="live-section" style="display:none">
      <h2>Live View</h2>
      <img id="live-img" alt="" style="display:none">
      <div id="live-placeholder">Waiting for snapshot...</div>
    </div>
    <div class="clip-list" id="clip-list"></div>
    <p class="empty" id="empty-msg" style="display:none">No clips yet. Start the daemon to begin monitoring.</p>
  </div>
</main>
<script>
async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  return r.json();
}

const _localLog = [];
function logLocal(msg) {
  const now = new Date().toLocaleTimeString('en-US', {hour:'numeric',minute:'2-digit',second:'2-digit',hour12:true});
  _localLog.unshift(`[${now}] ${msg}`);
}

async function daemonStart() { logLocal('Stream capture requested…'); await api('POST', '/daemon/start'); await refresh(); }
async function daemonStop()  { logLocal('Shutdown requested…');        await api('POST', '/daemon/stop');  await refresh(); }
async function recordNow()   { logLocal('Manual record requested…');   await api('POST', '/daemon/record'); await refresh(); }

async function saveConfig() {
  const cfg = {
    pre_roll:         parseInt(document.getElementById('cfg-preroll').value),
    clip_duration:    parseInt(document.getElementById('cfg-duration').value),
    motion_threshold: parseInt(document.getElementById('cfg-threshold').value),
    cooldown:         parseInt(document.getElementById('cfg-cooldown').value),
    camera_name:      document.getElementById('cfg-camera').value.trim(),
  };
  await api('POST', '/config', cfg);
}

async function deleteClip(name) {
  if (!confirm('Delete ' + name + '?')) return;
  await api('DELETE', '/clips/' + name);
  refresh();
}

let _lastClipNames = '';

function parseClipName(name) {
  // motion_20260402_160229.mp4 or manual_20260402_160229.mp4
  const m = name.match(/^(motion|manual)_(\\d{4})(\\d{2})(\\d{2})_(\\d{2})(\\d{2})(\\d{2})/);
  if (!m) return { label: name, type: 'clip', sub: '' };
  const [,type,yr,mo,dy,hr,mn,sc] = m;
  const d = new Date(yr, mo-1, dy, hr, mn, sc);
  const label = d.toLocaleString('en-US', {
    month:'short', day:'numeric', year:'numeric',
    hour:'numeric', minute:'2-digit', second:'2-digit', hour12:true
  });
  return { label, type, sub: type === 'motion' ? 'Motion triggered' : 'Manual recording' };
}

function fmtSize(bytes) {
  if (bytes < 1024*1024) return (bytes/1024).toFixed(0) + ' KB';
  return (bytes/1024/1024).toFixed(1) + ' MB';
}

function toggleClip(id) {
  const body = document.getElementById('body-' + id);
  if (!body) return;
  const isOpen = body.classList.contains('open');
  // Pause any open video before closing
  if (isOpen) {
    const v = body.querySelector('video');
    if (v) v.pause();
  }
  body.classList.toggle('open', !isOpen);
}

function renderClips(clips) {
  const list  = document.getElementById('clip-list');
  const empty = document.getElementById('empty-msg');

  const namesKey = clips.map(c => c.name).join(',');
  if (namesKey === _lastClipNames) return;  // nothing changed, don't touch the DOM
  _lastClipNames = namesKey;

  if (!clips.length) {
    list.innerHTML = '';
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  // Build a set of existing row IDs so we can add new ones without rebuilding
  const existing = new Set([...list.querySelectorAll('.clip-row')].map(el => el.dataset.name));
  const incoming = new Set(clips.map(c => c.name));

  // Remove rows no longer in the list
  for (const el of [...list.querySelectorAll('.clip-row')]) {
    if (!incoming.has(el.dataset.name)) el.remove();
  }

  // Prepend any new clips (newest first)
  for (const clip of [...clips].reverse()) {
    if (existing.has(clip.name)) continue;
    const id = clip.name.replace(/[^a-z0-9]/gi, '_');
    const { label, type, sub } = parseClipName(clip.name);
    const icon = type === 'motion' ? '🎯' : '⏺';
    const row = document.createElement('div');
    row.className = 'clip-row';
    row.dataset.name = clip.name;
    row.innerHTML = `
      <div class="clip-header" onclick="toggleClip('${id}')">
        <span class="clip-icon">${icon}</span>
        <div class="clip-meta">
          <div class="clip-date">${label}</div>
          <div class="clip-sub">${sub} &middot; ${fmtSize(clip.size)}</div>
        </div>
        <span style="color:#4b5563;font-size:.8rem">▶ Play</span>
      </div>
      <div class="clip-body" id="body-${id}">
        <video controls preload="none"><source src="/clips/${clip.name}" type="video/mp4"></video>
        <div class="clip-actions">
          <a class="btn btn-gray" href="/clips/${clip.name}" download style="text-decoration:none;font-size:.8rem">Download</a>
          <button class="btn btn-red" style="font-size:.8rem" onclick="deleteClip('${clip.name}')">Delete</button>
        </div>
      </div>`;
    list.prepend(row);
  }
}

function renderLog(entries) {
  const merged = [..._localLog, ...entries].slice(0, 40);
  document.getElementById('log-box').innerHTML = merged.map(e => `<p>${e}</p>`).join('');
}

let _liveRunning = false;

function renderStatus(s) {
  const dot = document.getElementById('dot');
  const label = document.getElementById('status-label');
  dot.className = 'dot' + (s.running ? ' on' : '');
  const badge = document.getElementById('rec-badge');
  badge.className = 'rec-badge' + (s.recording ? ' active' : '');
  label.textContent = s.running ? 'Monitoring' : 'Stopped';
  document.getElementById('cfg-preroll').value   = s.config.pre_roll;
  document.getElementById('cfg-duration').value  = s.config.clip_duration;
  document.getElementById('cfg-threshold').value = s.config.motion_threshold;
  document.getElementById('cfg-cooldown').value  = s.config.cooldown;
  document.getElementById('cfg-camera').value    = s.config.camera_name;
  const wasRunning = _liveRunning;
  _liveRunning = s.running;
  document.getElementById('live-section').style.display = s.running ? '' : 'none';
  if (s.running && !wasRunning) {
    // Reset live view to placeholder state when daemon starts
    const img = document.getElementById('live-img');
    img.src = '';
    img.style.display = 'none';
    document.getElementById('live-placeholder').style.display = '';
    refreshSnapshot();
  }
}

function refreshSnapshot() {
  if (!_liveRunning) return;
  const url = '/snapshot.jpg?t=' + Date.now();
  const tmp = new Image();
  tmp.onload = () => {
    const img = document.getElementById('live-img');
    img.src = tmp.src;
    img.style.display = '';
    document.getElementById('live-placeholder').style.display = 'none';
    setTimeout(refreshSnapshot, 2000);
  };
  tmp.onerror = () => setTimeout(refreshSnapshot, 2000);
  tmp.src = url;
}

async function refresh() {
  const s = await api('GET', '/status');
  renderStatus(s);
  renderLog(s.log);
  renderClips(s.clips);
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="blinkvault — Blink camera motion capture")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    args = parser.parse_args()
    print(f"blinkvault — open your browser to http://localhost:{args.port}")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
