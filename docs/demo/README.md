# 30-second demo (Pillar 4.2)

Self-contained HTML demo of the v2.0 wedge: **one memory, every AI tool, local-first.**

## See it now (no install)

```bash
open docs/demo/index.html
```

Or just double-click the file. Auto-plays on load. Click **↻ replay** to loop.

## What you're watching (30 seconds, 7 scenes)

| # | Time | Scene | Beat |
|---|---|---|---|
| 1 | 0–4s | Without codevira | Generic AI guess on "what did I decide about retries?" |
| 2 | 4–10s | 60-second install | `pipx install codevira && codevira setup` — 5 IDEs detected, 5 nudge files written, lifecycle hooks installed |
| 3 | 10–14s | With codevira | Same Claude Code, same question — specific answer citing the actual decision log |
| 4 | 14–20s | **Switch tools** | Cursor → Windsurf → Antigravity, all surface the SAME memory. **The wedge moment.** |
| 5 | 20–25s | Cross-tool sync | Edit in Claude Code; Cursor's AI sees the new decision automatically |
| 6 | 25–28s | End card | Install command + GitHub |
| 7 | 28–30s | Tagline | "One memory. Every AI tool. Local-first." |

## Record it as a video

The HTML is screen-recordable as-is. The stage is fixed at **1280×720**, so any tool that captures a window region works:

### macOS

```bash
# QuickTime — File → New Screen Recording → drag a 1280x720 region
# OR
brew install --cask kap
# Kap: select "Window" → click the demo browser tab → record
```

### Headless (chromium-headless + ffmpeg)

```bash
# In a fresh shell:
google-chrome --headless --disable-gpu --window-size=1280,720 \
              --screenshot=demo-frame-%06d.png \
              "file://$(pwd)/docs/demo/index.html"
# Then assemble frames with ffmpeg.
```

For Twitter / HN: 1280×720 @ 30fps yields ~3 MB MP4.

## What still needs polishing before a public release

The demo is **functional and on-message** — it tells the universality story end-to-end. For the actual launch video you'll likely want:

1. **Real screen captures** of Claude Code / Cursor / Windsurf in place of the styled HTML mocks (the mocks make the timing right but the launch video should show real IDE chrome).
2. **Voice-over** or captions — the wedge moment in scene 4 ("switch tools, same memory") benefits from an explicit audio call-out.
3. **Music bed** — a 30-second loop with a beat that lands on scene 4.
4. **End-card with real GitHub URL** — currently a placeholder `github.com/yourname/codevira`.
5. **A "no AI assistance from codevira" → "with codevira"** color/aesthetic shift between scenes 1 and 3 to drive the visual contrast harder.

This HTML version is the storyboard with timing locked. It runs in the browser, is recordable today, and is the source of truth for the shot list when the real video gets produced.

## Why a styled HTML mock instead of real screen recordings (yet)

- **Timing is locked**: every animation duration is enforced in CSS — no need to re-edit when retiming.
- **Contrast scenes 1 and 3**: side-by-side comparison is explicit. With real recordings, you'd need to control prompts identically across two takes — fragile.
- **Cross-tool grid (scene 4) is the wedge moment**: hard to capture three real IDEs simultaneously without staging. The mock makes the parallel obvious.
- **Iterate on the script in seconds**, not in a video editor.

When the real video gets recorded, re-use this HTML as the **shot list** and **on-screen timing reference**.
