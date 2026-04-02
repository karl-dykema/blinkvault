"""
Blink Doorbell MPEG-TS Livestream
Authenticates via blinkpy, starts a local MPEG-TS TCP proxy, and pipes
the live camera feed through ffmpeg.

Usage:
  python stream.py                          # auto-detect first camera
  python stream.py --camera "Front Door"    # pick camera by name
  python stream.py --output udp://127.0.0.1:1234  # UDP multicast/unicast
  python stream.py --output stream.ts       # write to file
  python stream.py --output pipe:1          # stdout (default)
"""

import asyncio
import argparse
import json
import ssl
import subprocess
import sys
import urllib.parse
from pathlib import Path

from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth, LoginError, BlinkTwoFARequiredError
from blinkpy.livestream import BlinkLiveStream
from blinkpy import api

CREDS_FILE = Path(__file__).parent / "creds.json"


# ---------------------------------------------------------------------------
# Patched livestream: fixes two blinkpy bugs
#   1. recv() uses read(n) which returns partial packets → use readexactly()
#   2. poll() kills the stream on first non-908 status → be tolerant
# ---------------------------------------------------------------------------

class ResilientLiveStream(BlinkLiveStream):
    async def recv(self):
        """Copy data from Blink cloud to all local TCP clients (readexactly fix)."""
        try:
            while not self.target_reader.at_eof():
                # Read the fixed 9-byte IMMI header
                try:
                    header = await self.target_reader.readexactly(9)
                except asyncio.IncompleteReadError:
                    break

                msgtype = header[0]
                payload_length = int.from_bytes(header[5:9], byteorder="big")

                if payload_length <= 0:
                    continue

                # Read the full payload
                try:
                    data = await self.target_reader.readexactly(payload_length)
                except asyncio.IncompleteReadError:
                    break

                # Only forward msgtype 0x00 MPEG-TS packets (start with 0x47)
                if msgtype != 0x00 or data[0] != 0x47:
                    continue

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
        """Poll the command API; tolerate transient errors instead of stopping."""
        consecutive_failures = 0
        max_failures = 5
        try:
            while not self.target_reader.at_eof():
                await asyncio.sleep(self.polling_interval)
                try:
                    response = await api.request_command_status(
                        self.camera.sync.blink,
                        self.camera.network_id,
                        self.command_id,
                    )
                    consecutive_failures = 0

                    for cmd in response.get("commands", []):
                        if cmd.get("id") == self.command_id:
                            if cmd.get("state_condition") not in ("new", "running"):
                                return  # stream legitimately ended

                except Exception:
                    consecutive_failures += 1
                    if consecutive_failures >= max_failures:
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
# Auth helpers
# ---------------------------------------------------------------------------

def load_creds() -> dict:
    if CREDS_FILE.exists():
        return json.loads(CREDS_FILE.read_text())
    return {}


def save_creds(auth: Auth) -> None:
    data = auth.login_attributes
    CREDS_FILE.write_text(json.dumps(data, indent=2))
    print(f"Credentials saved to {CREDS_FILE}", file=sys.stderr)


async def authenticate(blink: Blink) -> None:
    creds = load_creds()

    if creds:
        blink.auth = Auth(creds, no_prompt=True)
    else:
        blink.auth = Auth({"username": None, "password": None}, no_prompt=False)

    try:
        await blink.start()
        save_creds(blink.auth)
        return
    except BlinkTwoFARequiredError:
        pass
    except LoginError:
        print("Saved credentials failed; re-authenticating.", file=sys.stderr)
        CREDS_FILE.unlink(missing_ok=True)
        blink.auth = Auth({"username": None, "password": None}, no_prompt=False)
        try:
            await blink.start()
            save_creds(blink.auth)
            return
        except BlinkTwoFARequiredError:
            pass

    code = input("Enter the two-factor authentication code: ").strip()
    if not await blink.send_2fa_code(code):
        sys.exit("Two-factor authentication failed.")
    save_creds(blink.auth)


# ---------------------------------------------------------------------------
# Camera selection
# ---------------------------------------------------------------------------

def find_camera(blink: Blink, name: str | None):
    cameras = {}
    for sync in blink.sync.values():
        cameras.update(sync.cameras)

    if not cameras:
        sys.exit("No cameras found on this Blink account.")

    if name:
        cam = cameras.get(name)
        if cam is None:
            sys.exit(f"Camera '{name}' not found. Available: {', '.join(cameras)}")
        return cam

    cam_name, cam = next(iter(cameras.items()))
    print(f"Using camera: {cam_name}", file=sys.stderr)
    return cam


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

async def init_resilient_livestream(camera) -> ResilientLiveStream:
    """Like camera.init_livestream() but returns our patched subclass."""
    response = await api.request_camera_liveview(
        camera.sync.blink,
        camera.sync.network_id,
        camera.camera_id,
        camera_type=camera.camera_type,
    )
    if not response["server"].startswith("immis://"):
        sys.exit(f"Unsupported stream protocol: {response['server']}")
    return ResilientLiveStream(camera, response)


async def stream(camera, output: str) -> None:
    print("Requesting livestream from Blink...", file=sys.stderr)
    livestream = await init_resilient_livestream(camera)

    await livestream.start(host="127.0.0.1", port=0)
    url = livestream.url
    print(f"Local stream proxy at {url}", file=sys.stderr)

    feed_task = asyncio.create_task(livestream.feed())
    await asyncio.sleep(1.5)  # let the feed authenticate before ffmpeg connects

    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-y",
        "-analyzeduration", "10000000",
        "-probesize",        "10000000",
        "-i", url,
        "-map", "0:v:0",
        "-ss", "4",
        "-c:v", "copy",
        "-f", "mpegts",
        output,
    ]
    print(f"Running: {' '.join(cmd)}", file=sys.stderr)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: subprocess.run(cmd))

    feed_task.cancel()
    try:
        await feed_task
    except asyncio.CancelledError:
        pass
    livestream.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(description="Blink doorbell MPEG-TS livestream")
    parser.add_argument("--camera", help="Camera name (default: first found)")
    parser.add_argument(
        "--output", default="pipe:1",
        help="ffmpeg output: udp://127.0.0.1:1234  stream.ts  pipe:1 (default)",
    )
    args = parser.parse_args()

    blink = Blink(motion_interval=0, refresh_rate=30)
    await authenticate(blink)
    camera = find_camera(blink, args.camera)
    await stream(camera, args.output)

    if blink.auth and hasattr(blink.auth, "session") and blink.auth.session:
        await blink.auth.session.close()


if __name__ == "__main__":
    asyncio.run(main())


def cli():
    asyncio.run(main())
