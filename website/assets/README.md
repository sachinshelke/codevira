# `website/assets/` — what is source and what ships

Two kinds of SVG live here and they are **not** interchangeable.

## Diagram sources — edit the `.svg`, then re-render the `.png`

| source | rendered | embedded in |
|---|---|---|
| `blueprint.svg` | `blueprint.png` | README hero |
| `overview.svg` | `overview.png` | `index.html` "at a glance" |
| `architecture.svg` | `architecture.png` | README, `readme.html` |
| `enforcement-flow.svg` | `enforcement-flow.png` | README, `readme.html` |
| `internals.svg` | `internals.png` | README, `readme.html` |
| `file-layout.svg` | `file-layout.png` | README, `readme.html` |

**Nothing embeds these `.svg` files.** README.md and both site pages reference
the `.png` only, so editing the SVG alone changes nothing a reader will ever
see. The SVG is kept because it is the editable original — without it the next
diagram change means redrawing from a raster.

### Re-rendering

There is deliberately **no committed build step**: this repo ships a Python
package, and a diagram toolchain in `pyproject.toml` would be a dependency
every user installs to get something only a maintainer needs. The PNGs are
produced out-of-repo.

What the current PNGs were made with, so a future render matches:

- **headless Chrome**, at exactly **2×** the SVG's intrinsic size — the `<img>`
  and the window are both sized `W*2 × H*2` rather than using
  `--force-device-scale-factor`, which Chrome ignores on macOS
- then **palette-quantised to 8-bit indexed**, which is visually lossless on
  flat diagram art and cut the set from 3.4 MB to 1.6 MB

Any renderer is fine as long as the output stays 2× and self-contained. After
re-rendering, check the file actually shrank — a truecolour PNG of a flat
diagram is roughly twice the size it needs to be.

### Constraints these diagrams are built to

- **≤ 900 px intrinsic width** for anything embedded in the README. GitHub's
  content column is ~880 px, and a wider sheet is downscaled, which drags small
  type under the legibility floor.
- **≥ 10 px effective font size** after that downscale.
- **Self-contained** — no `<script>`, no external fonts, no remote refs.
- **A full-canvas background `<rect>`**, so the sheet does not go transparent
  and unreadable on GitHub's dark theme.
- Colours come from the site's light palette (`#f3f5f9` ground, white cards on
  `#dfe4ec` hairlines, `#0064d6` → `#5a5fe0` reserved for one focal element per
  diagram).

## Icons — the `.svg` ships as-is

`claude.svg`, `copilot.svg`, `gemini.svg`, `mcp.svg` are vendor marks used
directly at small sizes. They have no PNG twin and need no render step.

`windsurf.svg` is currently referenced by nothing — Windsurf is not a `setup`
target. Left in place rather than deleted, since removing an unused vendor mark
saves 1 KB and costs a re-download if the integration lands.

## README image URLs are absolute on purpose

`README.md` points at
`https://raw.githubusercontent.com/sachinshelke/codevira/main/website/assets/…`
rather than relative paths, because `pyproject.toml` sets
`readme = "README.md"` — the README **is** the PyPI long description, and PyPI
does not resolve relative image paths. They would render on GitHub and in every
local preview while silently breaking on the project page.

One consequence: a newly added image 404s until the commit is pushed, because
the URL resolves against `main`. Push before publishing to PyPI.

`website/readme.html` is the exception — it lives next to this directory, so it
uses relative paths.
