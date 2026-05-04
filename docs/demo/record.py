"""
record.py — Capture the 30-second demo HTML to MP4 via Playwright.

Usage:
    .venv/bin/python docs/demo/record.py

Output:
    docs/demo/codevira-demo.webm   (Playwright's native format)
    docs/demo/codevira-demo.mp4    (transcoded via ffmpeg if available)
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path


def main():
    here = Path(__file__).resolve().parent
    html = here / "index.html"
    if not html.exists():
        print(f"ERROR: {html} not found", file=sys.stderr)
        sys.exit(1)

    # Output directory for the recording
    out_dir = here / "_recording"
    out_dir.mkdir(exist_ok=True)

    # Stage size matches the demo's CSS (1280x720)
    WIDTH, HEIGHT = 1280, 720
    DURATION_S = 30  # auto-play runs for 30 seconds

    print(f"▸ Recording {DURATION_S}s at {WIDTH}x{HEIGHT}…")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            record_video_dir=str(out_dir),
            record_video_size={"width": WIDTH, "height": HEIGHT},
        )
        page = context.new_page()
        page.goto(html.as_uri())
        # The demo auto-plays after 500ms of load. Wait for the
        # full timeline (30s) plus a buffer to capture the final frame.
        time.sleep(DURATION_S + 2)
        page.close()
        # Closing the context flushes the video file
        context.close()
        browser.close()

    # Find the produced webm
    webms = sorted(out_dir.glob("*.webm"))
    if not webms:
        print("ERROR: no .webm produced", file=sys.stderr)
        sys.exit(1)
    webm = webms[-1]
    final_webm = here / "codevira-demo.webm"
    shutil.move(str(webm), str(final_webm))
    print(f"  ✓ wrote {final_webm} ({final_webm.stat().st_size:,} bytes)")

    # Transcode to mp4 if ffmpeg is available
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        mp4 = here / "codevira-demo.mp4"
        cmd = [
            ffmpeg, "-y",
            "-i", str(final_webm),
            "-c:v", "libx264",
            "-preset", "slow",
            "-crf", "20",
            "-pix_fmt", "yuv420p",  # required for QuickTime/most players
            "-movflags", "+faststart",
            str(mp4),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  ✓ transcoded → {mp4} ({mp4.stat().st_size:,} bytes)")
        else:
            print(
                f"  ⚠ ffmpeg failed; webm only.\n"
                f"  stderr:\n{result.stderr[-500:]}",
                file=sys.stderr,
            )
    else:
        print("  ⚠ ffmpeg not found on PATH; webm only")

    # Cleanup the empty recording dir
    try:
        out_dir.rmdir()
    except OSError:
        pass

    print("▸ done.")


if __name__ == "__main__":
    main()
