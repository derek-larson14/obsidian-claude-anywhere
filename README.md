# Claude Anywhere

Run Claude Code from any device (mobile, tablet) via a WebSocket relay to your Mac.

**Note:** This is designed for use with a physical keyboard (e.g., Bluetooth keyboard with tablet). Touch-only mobile use is not well supported - Claude Code's terminal interface requires keyboard-first interaction.


notes: i am not sure if the touch to close keyboard works, or when an external keyboard is on there how that is handled? also big thing w daylight will be can i use voice input as well. 

## How It Works

1. **Desktop (Mac):** Runs a relay server that spawns Claude Code in a PTY
2. **Mobile/Tablet:** Connects via WebSocket through the Obsidian plugin
3. **Sync:** Settings (server URL, auth token) sync via Obsidian Sync

## Quick Start

### Prerequisites
- Mac with Claude Code installed (`claude` command available)
- Obsidian with Obsidian Sync (for settings sync to mobile)
- Same WiFi network (LAN mode) OR Tailscale (remote mode)
- **Physical keyboard** for mobile/tablet use

### Desktop Setup (Mac)

1. Install the plugin in Obsidian
2. Go to Settings → Claude Anywhere
3. Enable "Remote Access"
4. Click "+ Trust this network"
5. Server starts automatically

**Keep your Mac awake:**
```bash
# Prevent sleep (run in terminal)
caffeinate -d -i -s
```

**Recommended:** Mac Mini as always-on server - low power, runs headless.

### Mobile/Tablet Setup

1. Install plugin in Obsidian (same vault, synced)
2. Wait for Obsidian Sync to sync settings
3. Connect Bluetooth keyboard
4. Open Claude Anywhere - connects automatically

## Modes

### LAN Mode (Default)
- **Pros:** Easy setup, no extra software
- **Cons:** Must be on same WiFi
- **Security:** Token auth (cryptographically secure, synced via Obsidian)
- **Best for:** Home use

### Tailscale Mode
- **Pros:** Works from anywhere, encrypted tunnel
- **Cons:** Requires Tailscale setup on all devices
- **Security:** WireGuard encryption + token auth
- **Best for:** Remote access, travel

**Tailscale Setup:**
1. Install Tailscale: https://tailscale.com/download
2. Sign in on Mac and mobile device
3. In plugin settings, switch to Tailscale mode
4. Server binds to Tailscale IP (100.x.x.x)

## Session Behavior

**On disconnect:** Claude session is killed (intentional - prevents orphan processes)

**To continue a conversation:** Use Claude's built-in `/resume` command after reconnecting. This is cleaner than trying to keep sessions alive across network drops.

## Security Notes

- **LAN mode:** Traffic is unencrypted (`ws://`). Use on trusted networks only.
- **Tailscale mode:** Traffic encrypted via WireGuard. Safe for any network.
- **Token auth:** Both modes require token authentication for defense-in-depth.
- **Token generation:** Uses cryptographically secure random bytes.

## Testing Checklist

### Desktop (Mac)
- [x] Server starts and stops
- [x] Terminal connects to server
- [x] Settings UI - trusted network card
- [x] Add/remove trusted network
- [x] Trust new network when on different WiFi
- [x] Fast plugin load (non-blocking server start)

### Tailscale Mode
- [x] Tailscale IP auto-detection (multiple fallback methods)
- [x] Server binds to Tailscale IP only
- [x] Mode switch (LAN ↔ Tailscale)

### Mobile (with keyboard)
- [x] Settings sync via Obsidian Sync
- [x] Terminal connects
- [x] Arrow key buttons for navigation
- [x] Escape key works in vim mode

## Known Limitations

### Mobile Touch UX
Touch-only interaction doesn't work well with terminal UX. Claude Code is designed for keyboard input. For mobile use, connect a Bluetooth keyboard.

### Display
- File references may get truncated on narrow screens
- Font size is reduced on mobile for more columns

## TODO / Future Features

### High Priority
- [ ] Server stops on plugin unload
- [ ] Connection from different WiFi via Tailscale (test end-to-end)

### Medium Priority
- [ ] Terminal prompt to trust network on first open
- [ ] Better Tailscale setup instructions in UI
- [ ] Multiple trusted networks (array instead of single)
- [ ] Desktop-to-desktop use case (use from Mac without Claude installed)

### Nice to Have
- [ ] Windows support
- [ ] Mac Mini setup guide
- [ ] Auto-caffeinate option
- [ ] LaunchAgent plist for auto-starting relay on Mac login

## Use Cases

**Primary:** Tablet with keyboard → Mac server
- Run Claude Code from your tablet (iPad, Android tablet, Daylight)
- Edit vault files remotely
- Works great with Bluetooth keyboard

**Secondary:** Desktop → Mac server
- Use from a second Mac without Claude Code installed
- Consistent environment (always uses server's Claude setup)

## Files

- `main.js` - Bundled Obsidian plugin
- `relay_server.py` - Python WebSocket relay (no external deps)
- `manifest.json` - Plugin metadata
- `styles.css` - Terminal styling

## Development

After changes, sync to test vault:
```bash
cp main.js styles.css manifest.json relay_server.py \
   ~/Github/exec/.obsidian/plugins/claude-anywhere/
```

## Architecture

```
Tablet/Mobile                    Mac (Server)
┌─────────────────┐              ┌─────────────────┐
│ Obsidian        │   WebSocket  │ Obsidian        │
│ + Plugin        │◄────────────►│ + Plugin        │
│ + xterm.js      │              │ + relay_server  │
│ + Keyboard      │              │ + Claude Code   │
└─────────────────┘              └─────────────────┘
```

**LAN Mode:** `ws://192.168.x.x:8765` (local network)
**Tailscale:** `ws://100.x.x.x:8765` (Tailscale network)
