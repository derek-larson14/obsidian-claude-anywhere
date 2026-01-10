# Claude Anywhere

Run Claude Code from any device (mobile, tablet) via a WebSocket relay to your Mac.

## How It Works

1. **Desktop (Mac):** Runs a relay server that spawns Claude Code in a PTY
2. **Mobile:** Connects via WebSocket through the Obsidian plugin
3. **Sync:** Settings (server URL, auth token) sync via Obsidian Sync

## Setup

### Desktop (Mac)

1. Install the plugin in Obsidian
2. Go to Settings → Claude Anywhere
3. Enable "Remote Access"
4. Click "+ Trust this network" (saves your home WiFi)
5. Server starts automatically

### Mobile

1. Install the plugin in Obsidian (same vault, synced)
2. Settings sync automatically via Obsidian Sync
3. Open Claude Anywhere - connects to your Mac

## Modes

### LAN Mode (Default)
- Works on same WiFi network
- Requires token authentication (auto-generated, synced)
- Trusted network prevents auto-start on untrusted WiFi

### Tailscale Mode
- Works from anywhere via Tailscale VPN
- No token needed (Tailscale handles auth)
- Requires Tailscale installed on both devices

## Testing Checklist

### Desktop (Mac)
- [x] Server starts and stops
- [x] Terminal connects to server
- [x] Settings UI - trusted network card
- [x] Add/remove trusted network
- [ ] Auto-start server on Obsidian launch
- [ ] Server stops on plugin unload
- [ ] Reconnection after disconnect

### Tailscale Mode
- [ ] Tailscale IP auto-detection
- [ ] Server binds to Tailscale IP only
- [ ] Mode switch (LAN ↔ Tailscale)
- [ ] Connection from different WiFi

### Mobile
- [ ] Settings sync via Obsidian Sync
- [ ] Terminal connects (LAN mode)
- [ ] Terminal connects (Tailscale mode)
- [ ] Error messages display correctly

### Token Auth
- [ ] Token auto-generated and saved
- [ ] Token syncs to mobile
- [ ] Server rejects wrong token
- [ ] Tailscale skips token check

## TODO

- [ ] Multiple trusted networks (array instead of single)
- [ ] Terminal prompt to trust network on first open
- [ ] Better Tailscale detection/error messages
- [ ] Windows support (currently macOS only)
- [ ] Rename trusted network inline (Obsidian modal)

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
