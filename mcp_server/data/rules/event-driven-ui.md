---
trigger: always_on
---

# Rule 017: Event-Driven UI Contract

## 1. Transport

- **SSE (Server-Sent Events)** is the ONLY event transport for v1.1
- No log parsing
- No stdout scraping
- No polling for state (except as fallback)

## 2. Event Requirements

Every event MUST include:
- `type`: Event type (health, step, error, heartbeat)
- `sequence_id`: Monotonic sequence number
- `timestamp`: ISO8601 timestamp
- `execution_id`: Trace correlation ID

## 3. Ordering Rules

| Rule | Description |
|------|-------------|
| Sequence Monotonic | Ignore events with `sequence_id` ≤ last processed |
| Late Event Grace | Accept events up to 5s late by timestamp |
| State Never Regresses | DONE → RUNNING is invalid, ignore |

## 4. Connection Parameters

| Parameter | Value |
|-----------|-------|
| Heartbeat interval | 2 seconds |
| Heartbeat timeout | 10 seconds |
| Reconnect delay | 1 second |
| Max reconnect attempts | 3 |

## 5. Failure Behavior

- After 3 consecutive failures: Show "Connection Lost" banner
- On reconnect: Resume from `Last-Event-ID`
- Never block UI on connection issues

## 6. Rendering

- Coalesce events to max 10 Hz
- Never block on event processing
- Queue events if needed

**Violation**: Log parsing, state regression, blocking event handler, untyped events.
