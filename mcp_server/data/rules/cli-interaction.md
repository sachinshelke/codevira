---
trigger: always_on
---

# Rule 015: CLI Interaction Contract

## 1. Keyboard Standards

All CLI TUI screens MUST follow these keybinding rules:

| Key | Action | Scope |
|-----|--------|-------|
| `q` / `Esc` | Exit / Cancel | Universal |
| `Ctrl+C` | Graceful abort with cleanup | Universal |
| `Enter` | Confirm selection / Execute | Context-specific |
| `↑` / `↓` | Navigate rows | Tables/Lists |
| `?` | Show help overlay | Universal |
| `r` | Force refresh | Status screens |

## 2. Discoverability

- **Footer Hint Bar**: Every active keybinding MUST appear in the footer
- **No Hidden Keybindings**: If a key does something, it must be visible
- **Help Overlay**: `?` opens inline help, `Esc` closes it

Footer format example:
```
[q] Quit  [r] Refresh  [?] Help  [↑↓] Navigate
```

## 3. Navigation Model

- v1.1 is **single-view only** (no modal stacking)
- One command = one full-screen view
- Focus follows deterministic order (top-to-bottom, left-to-right)

## 4. Confirmation Rules

| Action Type | Behavior |
|-------------|----------|
| Read-only | No confirmation |
| Mutating | Inline: `Press Enter to confirm, q to cancel` |
| Destructive | Typed: `Type 'DELETE' to confirm:` |

**Violation**: Hidden keybindings, silent mutations, nested modals.
