#!/usr/bin/env python3
"""
Claude Anywhere Relay Server

WebSocket server that bridges remote clients to Claude Code CLI via PTY.
Run this on your Mac, connect from any device with the modified Obsidian plugin.

No external dependencies - uses only Python standard library.
"""

import asyncio
import base64
import fcntl
import hashlib
import json
import os
import pty
import select
import signal
import struct
import subprocess
import termios
from pathlib import Path

# Configuration
PORT = 8765

# Sync block markers - Claude Code wraps large updates in these
# Stripping them prevents xterm.js from buffering massive atomic updates
SYNC_START = b'\x1b[?2026h'
SYNC_END = b'\x1b[?2026l'

def strip_sync_markers(data: bytes) -> bytes:
    """Remove synchronized update markers from terminal output."""
    return data.replace(SYNC_START, b'').replace(SYNC_END, b'')

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


class ClaudeSession:
    """Manages a single Claude Code PTY session."""

    def __init__(self):
        self.master_fd = None
        self.pid = None
        self.websocket = None
        self.read_task = None

    async def start(self, websocket, cwd=None, cols=80, rows=24):
        """Spawn Claude Code in a PTY and start relaying."""
        self.websocket = websocket
        self.master_fd, slave_fd = pty.openpty()

        # Set PTY size BEFORE forking so Claude starts with correct dimensions
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

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

                # Check for defaultFolder in plugin settings
                try:
                    settings_path = os.path.join(cwd, ".obsidian", "plugins", "claude-anywhere", "data.json")
                    if os.path.exists(settings_path):
                        with open(settings_path, 'r') as f:
                            settings = json.load(f)
                            default_folder = settings.get("defaultFolder", "").strip()
                            if default_folder:
                                folder_path = os.path.join(cwd, default_folder)
                                if os.path.isdir(folder_path):
                                    cwd = folder_path
                except (OSError, json.JSONDecodeError):
                    pass  # Ignore errors, use original cwd

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
        """Read from PTY and send to WebSocket using efficient async I/O."""
        loop = asyncio.get_event_loop()

        # Set non-blocking mode on the PTY
        flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
        fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        # Event to signal when data is ready
        data_ready = asyncio.Event()

        def on_readable():
            data_ready.set()

        # Register the FD with the event loop (efficient, no threads)
        loop.add_reader(self.master_fd, on_readable)

        try:
            while True:
                if not self.is_alive():
                    await self.send_status("session_ended", "Claude session ended")
                    break

                # Wait for data or timeout (for periodic liveness checks)
                try:
                    await asyncio.wait_for(data_ready.wait(), timeout=1.0)
                    data_ready.clear()
                except asyncio.TimeoutError:
                    continue  # Check is_alive and loop

                # Read all available data
                try:
                    data = os.read(self.master_fd, 4096)
                    if not data:
                        await self.send_status("session_ended", "Claude session ended")
                        break

                    # Strip sync markers to prevent xterm.js jerkiness
                    data = strip_sync_markers(data)

                    if self.websocket and self.websocket.open:
                        await self.websocket.send(data.decode("utf-8", errors="replace"))

                except BlockingIOError:
                    # No data available yet, wait for next event
                    continue
                except OSError as e:
                    await self.send_status("session_ended", f"Connection lost: {e}")
                    break

        except Exception as e:
            print(f"PTY read error: {e}", flush=True)
        finally:
            # Always clean up the reader
            try:
                loop.remove_reader(self.master_fd)
            except Exception:
                pass

        self.pid = None

    def write(self, data: str):
        """Write input to PTY."""
        if self.master_fd:
            # Filter out replacement characters (U+FFFD) that appear from
            # invalid UTF-8 sequences on Android keyboards
            data = data.replace('\ufffd', '')
            if data:  # Only write if there's something left
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


async def handle_client(reader, writer):
    """Handle a WebSocket client connection. Each client gets its own Claude session."""
    websocket = await WebSocketConnection.accept(reader, writer)
    if not websocket:
        return

    client_ip = websocket.remote_address[0]
    print(f"Client connected: {client_ip}", flush=True)

    # Each connection has its own session (not global)
    session = None
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
    except (asyncio.TimeoutError, json.JSONDecodeError) as e:
        print(f"No init message received: {e}", flush=True)

    async def send_status(status, message=""):
        await websocket.send(json.dumps({"type": "status", "status": status, "message": message}))

    async def start_new_session():
        nonlocal session
        if session:
            await session.stop()
        session = ClaudeSession()
        # Pass cols/rows so PTY is sized correctly BEFORE Claude starts
        await session.start(websocket, cwd=cwd, cols=cols, rows=rows)
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
                        # Store new dimensions for session restarts
                        cols = msg["cols"]
                        rows = msg["rows"]
                        if session:
                            session.resize(cols, rows)
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

            if session:
                session.write(message)

    except ConnectionError:
        print(f"Client disconnected: {client_ip}", flush=True)
    except Exception as e:
        print(f"Error: {e}", flush=True)
    finally:
        if session:
            print(f"Stopping Claude session for {client_ip}...", flush=True)
            await session.stop()
        await websocket.close()
        print(f"Connection closed: {client_ip}", flush=True)


async def main():
    """Main entry point."""
    print("Claude Anywhere Relay Server")
    print("=" * 40)

    tailscale_ip = get_tailscale_ip()
    if not tailscale_ip:
        print("ERROR: Tailscale not connected!")
        print("Please ensure Tailscale is running and connected.")
        return

    print(f"Tailscale IP: {tailscale_ip}")
    print(f"Listening on ws://{tailscale_ip}:{PORT}")
    print()

    server = await asyncio.start_server(handle_client, tailscale_ip, PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
