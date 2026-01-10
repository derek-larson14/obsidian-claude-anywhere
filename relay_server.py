#!/usr/bin/env python3
"""
Claude Anywhere Relay Server

WebSocket server that bridges remote clients to Claude Code CLI via PTY.
Run this on your Mac, connect from any device with the modified Obsidian plugin.
"""

import argparse
import asyncio
import fcntl
import json
import os
import pty
import secrets
import signal
import struct
import subprocess
import termios
import time
import websockets
from pathlib import Path
from websockets.server import serve

# Configuration
PORT = 8765
CLAUDE_CMD = ["claude"]  # The command to run
TOKEN_DIR = Path.home() / ".claude-anywhere"
TOKEN_FILE = TOKEN_DIR / "token"


def get_tailscale_ip():
    """Auto-detect Tailscale IP if running."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            ip = result.stdout.strip().split('\n')[0]
            if ip.startswith("100."):
                return ip
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass
    return None


def get_or_create_token():
    """Get token from env var, file, or create a new one."""
    # First check environment variable (set by Obsidian plugin)
    env_token = os.environ.get('CLAUDE_ANYWHERE_TOKEN')
    if env_token:
        return env_token

    # Fall back to file-based token
    TOKEN_DIR.mkdir(exist_ok=True)

    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()

    # Generate new token
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)  # Read/write only for owner
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

        # Create PTY
        self.master_fd, slave_fd = pty.openpty()

        # Fork process
        self.pid = os.fork()

        if self.pid == 0:
            # Child process: become Claude Code
            os.close(self.master_fd)
            os.setsid()
            os.dup2(slave_fd, 0)  # stdin
            os.dup2(slave_fd, 1)  # stdout
            os.dup2(slave_fd, 2)  # stderr
            os.close(slave_fd)

            # Change to vault directory if provided
            if cwd:
                # If path is relative (e.g., just "exec" from mobile), look in ~/Github/
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
                    pass  # Fall back to current dir if path invalid

            # Set TERM for proper terminal handling
            os.environ["TERM"] = "xterm-256color"

            # Execute Claude
            os.execvp(CLAUDE_CMD[0], CLAUDE_CMD)
        else:
            # Parent process: relay I/O
            os.close(slave_fd)

            # Keep blocking mode - we use select() to know when data is ready
            # Start reading from PTY
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
        print(f"Starting PTY read loop for fd={self.master_fd}", flush=True)

        while True:
            try:
                # Check if process is still alive
                if not self.is_alive():
                    print("Claude process exited", flush=True)
                    await self.send_status("session_ended", "Claude session ended")
                    break

                # Wait for data to be available (runs in thread pool)
                ready = await loop.run_in_executor(None, self._wait_for_read)
                if not ready:
                    continue

                # Read available data
                data = os.read(self.master_fd, 4096)
                if not data:
                    print("PTY EOF", flush=True)
                    await self.send_status("session_ended", "Claude session ended")
                    break

                # Send to WebSocket
                if self.websocket and self.websocket.open:
                    await self.websocket.send(data.decode("utf-8", errors="replace"))

            except OSError as e:
                print(f"PTY read OSError: {e}", flush=True)
                await self.send_status("session_ended", f"Connection lost: {e}")
                break
            except Exception as e:
                print(f"PTY read error: {e}", flush=True)
                break

        print("PTY read loop ended", flush=True)
        self.pid = None  # Mark session as dead

    def _wait_for_read(self):
        """Block until PTY has data (runs in executor). Returns True if data ready."""
        import select
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
                # pid=0 means child still running, pid>0 means exited
                if pid == 0:
                    return True
                else:
                    print(f"Claude process {self.pid} exited with status {status}", flush=True)
                    self.pid = None
                    return False
            except ChildProcessError:
                self.pid = None
                return False
        return False

    async def stop(self):
        """Stop the session (idempotent - safe to call multiple times)."""
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
                pass  # Already closed
            self.master_fd = None


# Global state
current_session = None  # type: ClaudeSession | None
require_token = True  # Set by main() based on mode
expected_token = None  # Set by main() for LAN mode


async def handle_client(websocket):
    """Handle a WebSocket client connection."""
    global current_session

    client_ip = websocket.remote_address[0]
    print(f"Client connected: {client_ip}", flush=True)

    # Wait for init message with cwd
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

                # Token validation for LAN mode
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

    # Always start fresh session (session persistence removed for reliability)
    print(f"Starting new Claude session in {cwd}...", flush=True)
    await start_new_session()

    try:
        async for message in websocket:
            # Check if it's a control message (JSON)
            if message.startswith("{"):
                try:
                    msg = json.loads(message)
                    if msg.get("type") == "resize":
                        print(f"Resize: {msg['cols']}x{msg['rows']}", flush=True)
                        current_session.resize(msg["cols"], msg["rows"])
                        continue
                    elif msg.get("type") == "init":
                        # Already handled above
                        continue
                    elif msg.get("type") == "restart":
                        # User requested session restart
                        print("Restart requested", flush=True)
                        await start_new_session()
                        continue
                    elif msg.get("type") == "ping":
                        await websocket.send(json.dumps({"type": "pong"}))
                        continue
                except json.JSONDecodeError:
                    pass

            # Regular input - send to PTY
            if current_session:
                current_session.write(message)

    except websockets.exceptions.ConnectionClosed:
        print(f"Client disconnected: {client_ip}", flush=True)
    except Exception as e:
        print(f"Error: {e}", flush=True)
    finally:
        # Clean up: stop Claude session when client disconnects
        if current_session:
            print("Stopping Claude session...", flush=True)
            await current_session.stop()
            current_session = None
        print("Connection closed", flush=True)


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Claude Anywhere Relay Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  LAN (default)   Listens on all interfaces, requires token auth.
                  Token is auto-generated and stored in ~/.claude-anywhere/token
                  Add this token to your Obsidian plugin settings.

  Tailscale       Listens only on Tailscale interface, no token needed.
                  Tailscale provides authentication.
        """
    )
    parser.add_argument(
        "--tailscale", "-t",
        action="store_true",
        help="Use Tailscale mode (binds to Tailscale IP, no token required)"
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=PORT,
        help=f"Port to listen on (default: {PORT})"
    )
    return parser.parse_args()


async def main():
    """Main entry point."""
    global require_token, expected_token

    args = parse_args()
    port = args.port

    print("Claude Anywhere Relay Server")
    print("=" * 40)

    if args.tailscale:
        # Tailscale mode: bind to Tailscale IP, no token needed
        tailscale_ip = get_tailscale_ip()
        if not tailscale_ip:
            print("ERROR: Tailscale not detected!")
            print("Make sure Tailscale is installed and running.")
            print("  Install: https://tailscale.com/download")
            print("  Then run: tailscale up")
            return

        host = tailscale_ip
        require_token = False
        print(f"Mode: Tailscale (secure)")
        print(f"Listening on ws://{host}:{port}")
        print()
        print("Only devices on your Tailscale network can connect.")
        print("No token required - Tailscale handles authentication.")
    else:
        # LAN mode: bind to all interfaces, require token
        host = "0.0.0.0"
        require_token = True
        expected_token = get_or_create_token()

        # Check if this is a new token
        token_age = TOKEN_FILE.stat().st_mtime if TOKEN_FILE.exists() else 0
        is_new_token = (time.time() - token_age) < 5  # Created in last 5 seconds

        print(f"Mode: LAN (token required)")
        print(f"Listening on ws://{host}:{port}")
        print()

        if is_new_token:
            print("NEW TOKEN GENERATED!")
            print()

        print(f"Token: {expected_token}")
        print()
        print("Add this token to your Obsidian plugin settings.")
        print("It will sync to all your devices via Obsidian Sync.")

    print()
    print("-" * 40)
    print()

    async with serve(handle_client, host, port):
        await asyncio.Future()  # Run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
