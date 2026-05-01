# 30-second screencast script

Open `examples/features_demo.rdl` in VSCode. Set window to 1920×1080 or
1600×900. Hide the activity bar (`View → Appearance → Activity Bar`)
and minimap (`View → Toggle Minimap`) so the editor pane is the focus.
Side-by-side memory map will appear via `Ctrl+Shift+V`.

Record at 1080p, 30 fps. Tools: OBS Studio (free), Loom, Windows native
Game Bar (Win+G), or macOS Cmd+Shift+5.

Recommended cursor speed: deliberate. Don't rush — the viewer reacts
on every action and a fast viewer is more impressive than fast cursor.

## Timeline

| Time   | Action | Screen state |
|--------|--------|--------------|
| 0.0 s  | Cursor lands on **line 121** (`addrmap top { … }`). | Editor only, viewer not open yet. |
| 1.0 s  | `Ctrl+Shift+V`. Memory Map panel opens beside. | Tabs visible — `top`, `demo_bridge`, `dpa_demo`. |
| 3.0 s  | Click **`top`** tab. Tree expands `addrmap top`. | Tree shows `rx_count`, `link_a`, `link_b`, `LCR`, `CTRL`, `PR16`, `PR8`, `WIDE_REG`, `optional_block`. |
| 5.0 s  | Click **`LCR`** in the tree. Editor scrolls to line 116 with a 200 ms accent flash. Detail pane shows the `uart_lcr_t` register. | Bit grid renders with `baud[1:0]`, `div[15:8]`, `eight_bit[16:16]`. |
| 8.0 s  | In Detail panel, expand the `enum · 4 values` disclosure under `baud`. The `power_state`-like enum table appears: `0x0 BAUD_9600`, `0x1 BAUD_19200`, `0x2 BAUD_115200`, `0x3 BAUD_USER`. | Encode-enum table visible. |
| 11.0 s | Click into the **register binary decoder** input. Type `0x12345`. | Per-field breakdown appears live: `baud=0x1 · BAUD_19200`, `div=0x23`, `eight_bit=0x0`. |
| 14.0 s | In editor, `F12` on **`my_counter_t`** at line 67 (in `dma_engine` addrmap). Editor jumps to its declaration at line 56. | Counter type definition visible. |
| 17.0 s | Hover on **`count[31:0]`** at line 60. Tooltip shows: address, width, access, reset, counter badge. | Hover popup. |
| 20.0 s | Switch tabs: click **`demo_bridge`** in the viewer. Tree updates. | New tab active, tree shows `side_a`, `side_b` nested addrmaps. Hover on `demo_bridge` shows `· bridge`. |
| 23.0 s | Switch VSCode color theme via `Ctrl+K Ctrl+T` → pick **Solarized Light** or **GitHub Light**. | Editor and Memory Map both transition palettes simultaneously. |
| 27.0 s | Cursor in editor on **`enable[0:0]`** field at line 100. Tree auto-selects matching node in viewer (cursor → tree sync, D10). | Tree highlight follows. |
| 29.5 s | Pause on the synced state. | Final frame for 0.5 s before fade. |

## Narration / overlay text (optional)

Skip narration if you're going for a silent demo. If you want overlays:

- 0–3 s: **"Live SystemRDL diagnostics + memory map for VSCode"**
- 8–11 s: **"Click any register → editor jumps. Click any field's `encode` → decoded enum table."**
- 14–17 s: **"F12 on a type, hover for resolved address/access/reset."**
- 23–27 s: **"Theme follows your VSCode color theme automatically."**

## After recording

```bash
# Convert MP4 → GIF (high-quality, ~3 MB)
ffmpeg -i demo.mp4 -vf "fps=15,scale=1280:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" demo.gif
```

Drop `demo.gif` (or `demo.mp4`) at `docs/demo.gif`. Reference it in the
root README:

```markdown
![demo](docs/demo.gif)
```

…directly under the heading.

## Common pitfalls

- **Cursor jumps**. Hide accessibility cursor highlights (Loom adds them
  by default — turn off "show clicks").
- **Personal info**. Hide your `~/projects/` path in the title bar — set
  VSCode's `window.title` to a generic value before recording.
- **Notifications**. Disable Slack/Discord/email notifications. Use
  Windows "Focus assist" or macOS "Do Not Disturb".
- **Compression**. GIF caps file size at ~3 MB for GitHub previews. If
  it's larger, upload as MP4 to the Release page and link.
