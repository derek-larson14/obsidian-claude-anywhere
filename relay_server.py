#!/usr/bin/env python3
"""
Claude Anywhere Relay Server

WebSocket server that bridges remote clients to Claude Code CLI via PTY.
Run this on your Mac, connect from any device with the modified Obsidian plugin.

No external dependencies - uses only Python standard library.
"""

import argparse
import asyncio
import base64
import fcntl
import hashlib
import json
import os
import pty
import secrets
import select
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path

# Configuration
PORT = 8765
TOKEN_DIR = Path.home() / ".claude-anywhere"
TOKEN_FILE = TOKEN_DIR / "token"

def find_claude():
    """Find the claude executable."""
    # Common locations
    paths = [
        "/opt/homebrew/bin/claude",  # macOS ARM Homebrew
        "/usr/local/bin/claude",      # macOS Intel Homebrew
        os.path.expanduser("~/.local/bin/claude"),  # pip install --user
        "/usr/bin/claude",
    ]
    for p in paths:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    # Fallback: hope it's in PATH
    return "claude"

CLAUDE_CMD = [find_claude()]

# WebSocket constants
WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OPCODE_TEXT = 0x1
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


class WebSocketConnection:
    """Minimal WebSocket implementation using standard library."""

    def __init__(self, reader, writer, remote_address):
        self.reader = reader
        self.writer = writer
        self.remote_address = remote_address
        self.open = True

    @classmethod
    async def accept(cls, reader, writer):
        """Perform WebSocket handshake and return connection."""
        remote_address = writer.get_extra_info('peername')

        # Read HTTP request
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = await reader.read(1024)
            if not chunk:
                return None
            request += chunk

        # Parse headers
        headers = {}
        lines = request.decode('utf-8', errors='replace').split('\r\n')
        for line in lines[1:]:
            if ':' in line:
                key, value = line.split(':', 1)
                headers[key.strip().lower()] = value.strip()

        # Validate WebSocket upgrade request
        ws_key = headers.get('sec-websocket-key')
        if not ws_key:
            writer.close()
            return None

        # Compute accept key
        accept_key = base64.b64encode(
            hashlib.sha1(ws_key.encode() + WS_MAGIC).digest()
        ).decode()

        # Send handshake response
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_key}\r\n"
            "\r\n"
        )
        writer.write(response.encode())
        await writer.drain()

        return cls(reader, writer, remote_address)

    async def recv(self):
        """Receive a WebSocket message."""
        if not self.open:
            raise ConnectionError("Connection closed")

        # Read frame header
        header = await self.reader.read(2)
        if len(header) < 2:
            self.open = False
            raise ConnectionError("Connection closed")

        fin = (header[0] >> 7) & 1
        opcode = header[0] & 0x0F
        masked = (header[1] >> 7) & 1
        payload_len = header[1] & 0x7F

        # Handle extended payload length
        if payload_len == 126:
            ext = await self.reader.read(2)
            payload_len = struct.unpack(">H", ext)[0]
        elif payload_len == 127:
            ext = await self.reader.read(8)
            payload_len = struct.unpack(">Q", ext)[0]

        # Read mask key if present
        mask_key = None
        if masked:
            mask_key = await self.reader.read(4)

        # Read payload
        payload = await self.reader.read(payload_len)

        # Unmask payload if masked
        if mask_key:
            payload = bytes(payload[i] ^ mask_key[i % 4] for i in range(len(payload)))

        # Handle control frames
        if opcode == OPCODE_CLOSE:
            self.open = False
            raise ConnectionError("Connection closed by client")
        elif opcode == OPCODE_PING:
            await self._send_frame(OPCODE_PONG, payload)
            return await self.recv()  # Get next message
        elif opcode == OPCODE_PONG:
            return await self.recv()  # Ignore pongs, get next message

        return payload.decode('utf-8', errors='replace')

    async def send(self, message):
        """Send a WebSocket text message."""
        if not self.open:
            return
        if isinstance(message, str):
            message = message.encode('utf-8')
        await self._send_frame(OPCODE_TEXT, message)

    async def _send_frame(self, opcode, payload):
        """Send a WebSocket frame."""
        frame = bytearray()

        # First byte: FIN + opcode
        frame.append(0x80 | opcode)

        # Second byte: payload length (no mask for server->client)
        length = len(payload)
        if length < 126:
            frame.append(length)
        elif length < 65536:
            frame.append(126)
            frame.extend(struct.pack(">H", length))
        else:
            frame.append(127)
            frame.extend(struct.pack(">Q", length))

        # Payload
        frame.extend(payload)

        self.writer.write(bytes(frame))
        await self.writer.drain()

    async def close(self):
        """Close the WebSocket connection."""
        if self.open:
            self.open = False
            try:
                await self._send_frame(OPCODE_CLOSE, b"")
                self.writer.close()
                await self.writer.wait_closed()
            except:
                pass


def get_tailscale_ip():
    """Auto-detect Tailscale IP if running."""
    # Method 1: Try tailscale CLI (with full path for GUI apps)
    tailscale_paths = [
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/usr/local/bin/tailscale",
        "tailscale"
    ]
    for ts_path in tailscale_paths:
        try:
            result = subprocess.run(
                [ts_path, "ip", "-4"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                ip = result.stdout.strip().split('\n')[0]
                if ip.startswith("100."):
                    return ip
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            continue

    # Method 2: Parse network interfaces for Tailscale IP (utun interfaces)
    try:
        result = subprocess.run(
            ["ifconfig"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            # Look for 100.x.x.x addresses (Tailscale CGNAT range)
            import re
            matches = re.findall(r'inet (100\.\d+\.\d+\.\d+)', result.stdout)
            if matches:
                return matches[0]
    except Exception:
        pass

    return None


def get_or_create_token():
    """Get token from env var, file, or create a new one."""
    env_token = os.environ.get('CLAUDE_ANYWHERE_TOKEN')
    if env_token:
        return env_token

    TOKEN_DIR.mkdir(exist_ok=True)

    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()

    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    return token


class ClaudeSession:
    """Manages a single Claude Code PTY session."""

    def __init__(self):
        self.master_fd = None
        self.pid = None
        self.websocket = None
        self.read_task = None

    async def start(self, websocket, cwd=None):
        """Spawn Claude Code in a PTY and start relaying."""
        self.websocket = websocket
        self.master_fd, slave_fd = pty.openpty()
        self.pid = os.fork()

        if self.pid == 0:
            # Child process
            os.close(self.master_fd)
            os.setsid()
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            os.close(slave_fd)

            if cwd:
                if not os.path.isabs(cwd):
                    base_paths = [
                        os.path.expanduser(f"~/Github/{cwd}"),
                        os.path.expanduser(f"~/{cwd}"),
                        cwd
                    ]
                    for try_path in base_paths:
                        if os.path.isdir(try_path):
                            cwd = try_path
                            break
                try:
                    os.chdir(cwd)
                except OSError:
                    pass

            os.environ["TERM"] = "xterm-256color"
            os.execvp(CLAUDE_CMD[0], CLAUDE_CMD)
        else:
            os.close(slave_fd)
            self.read_task = asyncio.create_task(self._read_pty())

    async def send_status(self, status, message=""):
        """Send a status message to the client."""
        if self.websocket and self.websocket.open:
            await self.websocket.send(json.dumps({
                "type": "status",
                "status": status,
                "message": message
            }))

    async def _read_pty(self):
        """Read from PTY and send to WebSocket."""
        loop = asyncio.get_event_loop()

        while True:
            try:
                if not self.is_alive():
                    await self.send_status("session_ended", "Claude session ended")
                    break

                ready = await loop.run_in_executor(None, self._wait_for_read)
                if not ready:
                    continue

                data = os.read(self.master_fd, 4096)
                if not data:
                    await self.send_status("session_ended", "Claude session ended")
                    break

                if self.websocket and self.websocket.open:
                    await self.websocket.send(data.decode("utf-8", errors="replace"))

            except OSError as e:
                await self.send_status("session_ended", f"Connection lost: {e}")
                break
            except Exception:
                break

        self.pid = None

    def _wait_for_read(self):
        """Block until PTY has data."""
        readable, _, _ = select.select([self.master_fd], [], [], 0.5)
        return len(readable) > 0

    def write(self, data: str):
        """Write input to PTY."""
        if self.master_fd:
            os.write(self.master_fd, data.encode("utf-8"))

    def resize(self, cols: int, rows: int):
        """Resize the PTY."""
        if self.master_fd:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

    def is_alive(self) -> bool:
        """Check if Claude process is still running."""
        if self.pid:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
                if pid == 0:
                    return True
                self.pid = None
                return False
            except ChildProcessError:
                self.pid = None
                return False
        return False

    async def stop(self):
        """Stop the session."""
        if self.read_task:
            self.read_task.cancel()
            try:
                await self.read_task
            except asyncio.CancelledError:
                pass
            self.read_task = None

        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
                await asyncio.sleep(0.5)
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.pid = None

        if self.master_fd:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None


# Global state
current_session = None
require_token = True
expected_token = None


async def handle_client(reader, writer):
    """Handle a WebSocket client connection."""
    global current_session

    websocket = await WebSocketConnection.accept(reader, writer)
    if not websocket:
        return

    client_ip = websocket.remote_address[0]
    print(f"Client connected: {client_ip}", flush=True)

    cwd = None
    cols = 80
    rows = 24

    try:
        init_msg = await asyncio.wait_for(websocket.recv(), timeout=5.0)
        if init_msg.startswith("{"):
            msg = json.loads(init_msg)
            if msg.get("type") == "init":
                cwd = msg.get("cwd")
                cols = msg.get("cols", 80)
                rows = msg.get("rows", 24)
                print(f"Init: cwd={cwd}, cols={cols}, rows={rows}", flush=True)

                if require_token:
                    client_token = msg.get("token", "")
                    if client_token != expected_token:
                        print(f"Auth failed: invalid token from {client_ip}", flush=True)
                        await websocket.send(json.dumps({
                            "type": "status",
                            "status": "auth_failed",
                            "message": "Invalid or missing token"
                        }))
                        await websocket.close()
                        return
                    print(f"Auth successful for {client_ip}", flush=True)
    except (asyncio.TimeoutError, json.JSONDecodeError) as e:
        print(f"No init message received: {e}", flush=True)
        if require_token:
            await websocket.close()
            return

    async def send_status(status, message=""):
        await websocket.send(json.dumps({"type": "status", "status": status, "message": message}))

    async def start_new_session():
        global current_session
        await send_status("starting", "Starting Claude...")
        if current_session:
            await current_session.stop()
        current_session = ClaudeSession()
        await current_session.start(websocket, cwd=cwd)
        if cwd:
            current_session.resize(cols, rows)
        await send_status("ready", "Claude is ready")

    print(f"Starting new Claude session in {cwd}...", flush=True)
    await start_new_session()

    try:
        while websocket.open:
            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                continue

            if message.startswith("{"):
                try:
                    msg = json.loads(message)
                    if msg.get("type") == "resize":
                        current_session.resize(msg["cols"], msg["rows"])
                        continue
                    elif msg.get("type") == "init":
                        continue
                    elif msg.get("type") == "restart":
                        await start_new_session()
                        continue
                    elif msg.get("type") == "ping":
                        await websocket.send(json.dumps({"type": "pong"}))
                        continue
                except json.JSONDecodeError:
                    pass

            if current_session:
                current_session.write(message)

    except ConnectionError:
        print(f"Client disconnected: {client_ip}", flush=True)
    except Exception as e:
        print(f"Error: {e}", flush=True)
    finally:
        if current_session:
            print("Stopping Claude session...", flush=True)
            await current_session.stop()
            current_session = None
        await websocket.close()
        print("Connection closed", flush=True)


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Claude Anywhere Relay Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  LAN (default)   Listens on all interfaces, requires token auth.
  Tailscale       Listens only on Tailscale interface, no token needed.
        """
    )
    parser.add_argument("--tailscale", "-t", action="store_true",
                        help="Use Tailscale mode")
    parser.add_argument("--port", "-p", type=int, default=PORT,
                        help=f"Port to listen on (default: {PORT})")
    return parser.parse_args()


async def main():
    """Main entry point."""
    global require_token, expected_token

    args = parse_args()
    port = args.port

    print("Claude Anywhere Relay Server")
    print("=" * 40)

    # Always require token for defense-in-depth security
    require_token = True
    expected_token = get_or_create_token()

    if args.tailscale:
        tailscale_ip = get_tailscale_ip()
        if not tailscale_ip:
            print("ERROR: Tailscale not detected!")
            return

        host = tailscale_ip
        print(f"Mode: Tailscale (encrypted + token)")
        print(f"Listening on ws://{host}:{port}")
    else:
        host = "0.0.0.0"
        print(f"Mode: LAN (token required)")
        print(f"Listening on ws://{host}:{port}")

    print(f"Token: {expected_token}")

    print()

    server = await asyncio.start_server(handle_client, host, port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
