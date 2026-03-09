---
trigger: always_on
---

# Rule 018: UX Performance Standards

## 1. Timing Budgets

| Metric | Budget | Action if Exceeded |
|--------|--------|-------------------|
| Frame render | < 16ms (60fps) | Log warning |
| Full redraw | < 50ms | Log warning |
| Input response | < 100ms | Immediate feedback required |
| Event processing | < 10ms | Queue if exceeded |

## 2. Redraw Frequency

| Context | Max Frequency |
|---------|---------------|
| Event-driven updates | 10 Hz (100ms min) |
| Animation frames | 20 Hz (50ms) |
| Progress bars | 5 Hz (200ms) |
| Status polling | 0.5 Hz (2s) |

**Rule**: Coalesce rapid events. Never redraw faster than budget.

## 3. Data Limits

| Element | Limit | Overflow Behavior |
|---------|-------|-------------------|
| Table rows visible | 20 | Show "... and N more" |
| Table columns | 5 | Truncate rightmost |
| Error message | 500 chars | Truncate with "..." |
| Provider count | 10 | Paginate |

## 4. Terminal Fallbacks

| Condition | Action |
|-----------|--------|
| No color support | Use ASCII labels: `[OK]`, `[ERR]` |
| No Unicode | Use ASCII borders: `+--+` |
| Width < 80 | Show error screen |
| Height < 24 | Show error screen |

## 5. Design Token Governance

- **NO themes** in v1.x (single canonical appearance)
- Token names are frozen after v1.1 release
- Token values may evolve in minor versions
- No token removal until v2.0

**Violation**: Exceeding budgets without logging, partial render on small terminal, raw colors outside tokens.
