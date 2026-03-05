---
trigger: always_on
---

# Rule 016: CLI Layout Composition

## 1. Screen Structure (Mandatory)

Every TUI screen MUST follow this structure:

```
╭─────────────────────────────────────────────╮
│  HEADER (1-3 lines)                         │
├─────────────────────────────────────────────┤
│  BODY (flexible, fills remaining space)     │
├─────────────────────────────────────────────┤
│  FOOTER (1 line: keybinding hints only)     │
╰─────────────────────────────────────────────╯
```

## 2. Zone Rules

- **Header**: Title, status badge, breadcrumb (1-3 lines fixed)
- **Body**: Primary content, fills `remaining - 4` lines
- **Footer**: Keybinding hints only (1 line fixed)

**Critical**: Zones NEVER collapse. If content overflows, truncate with ellipsis.

## 3. Terminal Constraints

| Constraint | Requirement |
|------------|-------------|
| Min width | 80 columns |
| Min height | 24 rows |
| Below minimum | Show error: "Terminal too small (need 80x24)" |

## 4. Nesting Limits

- Max panel depth: 1 (no nested panels)
- Max horizontal splits: 2
- Max vertical splits: 3

## 5. Redraw Policy

| Trigger | Redraw Type |
|---------|-------------|
| Event received | Partial (affected component only) |
| Terminal resize | Full (clear + repaint) |
| Focus change | Partial (update indicators) |
| Error condition | Full (switch to error screen) |

**Rule**: Same data MUST produce identical output every time (deterministic).

**Violation**: Collapsing zones, non-deterministic layout, deep nesting, partial render on small terminal.
