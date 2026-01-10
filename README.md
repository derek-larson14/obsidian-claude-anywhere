# Claude Anywhere

Run Claude Code from any device (mobile, tablet) via a WebSocket relay to your Mac.

## How It Works

1. **Desktop (Mac):** Runs a relay server that spawns Claude Code in a PTY
2. **Mobile:** Connects via WebSocket through the Obsidian plugin
3. **Sync:** Settings (server URL, auth token) sync via Obsidian Sync

## Quick Start

### Prerequisites
- Mac with Claude Code installed (`claude` command available)
- Obsidian with Obsidian Sync (for settings sync to mobile)
- Same WiFi network (LAN mode) OR Tailscale (remote mode)

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

### Mobile Setup

1. Install plugin in Obsidian (same vault, synced)
2. Wait for Obsidian Sync to sync settings
3. Open Claude Anywhere - connects automatically

## Modes

### LAN Mode (Default)
- **Pros:** Easy setup, no extra software
- **Cons:** Must be on same WiFi
- **Security:** Token auth (auto-generated, synced via Obsidian)
- **Best for:** Home use

### Tailscale Mode
- **Pros:** Works from anywhere, very secure
- **Cons:** Requires Tailscale setup on all devices
- **Security:** Tailscale handles auth (no token needed)
- **Best for:** Remote access, travel

**Tailscale Setup:**
1. Install Tailscale: https://tailscale.com/download
2. Sign in on Mac and mobile device
3. In plugin settings, switch to Tailscale mode
4. Server binds to Tailscale IP (100.x.x.x)

## Known Bugs

### Mobile Touch Issues
- [ ] Touching anywhere auto-focuses input (should only focus when touching input area)
- [ ] Can't scroll terminal without triggering keyboard
- [ ] Keyboard should close when touching outside input

### Display Issues
- [ ] File tags getting cut off (sizing issue)
- [ ] Can't see full filenames being referenced

### Connection Issues
- [ ] "Tap to reconnect" doesn't work - only Enter key works
- [ ] Tapping should trigger reconnect on mobile

### Keyboard/Input Issues
- [ ] Double-escape for vim mode (escape → escape to exit insert mode)

## Session Behavior

**On disconnect:** Claude session is killed (intentional - prevents orphan processes)

**To continue a conversation:** Use Claude's built-in `/resume` command after reconnecting. This is cleaner than trying to keep sessions alive across network drops.

## Testing Checklist

### Desktop (Mac)
- [x] Server starts and stops
- [x] Terminal connects to server
- [x] Settings UI - trusted network card
- [x] Add/remove trusted network
- [x] Trust new network when on different WiFi
- [ ] Auto-start server on Obsidian launch
- [ ] Server stops on plugin unload

### Tailscale Mode
- [ ] Tailscale IP auto-detection
- [ ] Server binds to Tailscale IP only
- [ ] Mode switch (LAN ↔ Tailscale)
- [ ] Connection from different WiFi via Tailscale

### Mobile
- [ ] Settings sync via Obsidian Sync
- [ ] Terminal connects (LAN mode, same WiFi)
- [ ] Terminal connects (Tailscale mode)
- [ ] Error messages display correctly
- [ ] Touch/tap interactions work properly

### Token Auth
- [ ] Token auto-generated and saved
- [ ] Token syncs to mobile
- [ ] Server rejects wrong/missing token
- [ ] Tailscale mode skips token check

## TODO / Future Features

### High Priority
- [ ] Fix mobile touch handling (scroll vs input focus)
- [ ] Fix "tap to reconnect" on mobile
- [ ] Fix display sizing/cutoff issues
- [ ] Test and fix Tailscale mode end-to-end

### Medium Priority
- [ ] Terminal prompt to trust network on first open
- [ ] Better Tailscale setup instructions in UI
- [ ] Help users decide LAN vs Tailscale (wizard?)
- [ ] Multiple trusted networks (array instead of single)

### Nice to Have
- [ ] Windows support
- [ ] Rename trusted network inline (Obsidian modal)
- [ ] Mac Mini setup guide
- [ ] Auto-caffeinate option
- [ ] LaunchAgent plist for auto-starting relay on Mac login
- [ ] Desktop-to-desktop use case (use from Mac without Claude installed)

## Use Cases

**Primary:** Mobile (iOS/Android) → Mac server
- Run Claude Code from your phone/tablet
- Edit vault files remotely

**Secondary:** Desktop → Mac server
- Use from a second Mac without Claude Code installed
- Consistent environment (always uses server's Claude setup)
- Could be useful for shared team setup

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
Mobile Device                    Mac (Server)
┌─────────────────┐              ┌─────────────────┐
│ Obsidian        │   WebSocket  │ Obsidian        │
│ + Plugin        │◄────────────►│ + Plugin        │
│ + xterm.js      │              │ + relay_server  │
└─────────────────┘              │ + Claude Code   │
                                 └─────────────────┘
```

**LAN Mode:** `ws://192.168.x.x:8765` (local network)
**Tailscale:** `ws://100.x.x.x:8765` (Tailscale network)
