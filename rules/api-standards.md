---
trigger: always_on
---

# Rule 019: API Standards

## Scope
This rule applies to ALL API endpoints. Every HTTP-facing service MUST follow these standards without exception.

## 1. Response Envelope (Mandatory)

Every API endpoint MUST return a consistent response envelope:

```python
{
    "success": true|false,
    "data": { ... },          # Present on success
    "error": {                # Present on failure
        "code": "ERROR_CODE",
        "message": "Human-readable message",
        "details": null
    },
    "meta": {                 # Optional (pagination, request_id, etc.)
        "request_id": "uuid",
        "page": 1,
        "total": 42
    }
}
```

- **No raw dicts**: Endpoints MUST NOT return untyped `dict` responses. Use typed models (Pydantic, dataclasses, or language-appropriate typed structs).
- **No mixed success**: If the operation failed, `success` MUST be `false` with an appropriate HTTP status code. Do NOT return `success: true` with failure details inside `data`.

## 2. Secret Masking (CRITICAL)

- **Connection strings**: API responses MUST NEVER expose raw connection strings containing passwords. Use masking: `postgres://user:***@host:5432/db`.
- **API keys / tokens**: Any response containing secrets MUST mask them before serialization.
- **Logs**: Structured logs MUST NOT contain raw secrets.

## 3. CORS

- **Mandatory**: CORS middleware MUST be enabled on every API deployment.
- **Configurable**: Origins controlled via environment variable (comma-separated, default: `*` for dev).
- **Expose Headers**: `X-Request-ID` and version headers MUST be in `expose_headers`.

## 4. Request Traceability

- **X-Request-ID**: Every API response MUST include an `X-Request-ID` header (auto-generated UUID if not provided by client).
- **Logging**: The `request_id` MUST be injected into the logging context for all downstream operations.
- **Correlation**: When a request triggers async work, the `request_id` MUST propagate into the event payload.

## 5. Health Endpoint Standards

- **Path**: `GET /health`
- **200**: Service is healthy and ready to serve requests.
- **503**: Service is unhealthy (e.g., dependency unreachable, disk full). Return structured error explaining what failed.
- **Must be unauthenticated**: Health probes from K8s/Docker MUST NOT require auth.

## 6. Async Job Pattern

For any long-running operation:

1. `POST` endpoint returns `202 Accepted` with a `job_id`.
2. Status available via `GET /v1/jobs/{id}` (typed response, not raw storage dump).
3. Real-time updates available via SSE stream.
4. Frontend SHOULD use SSE, MAY fall back to polling.

## 7. HTTP Status Codes

| Situation | Status Code |
|-----------|-------------|
| Resource created | 201 |
| Async job accepted | 202 |
| Resource deleted | 204 (no body) |
| Validation failure | 422 |
| Resource not found | 404 |
| Operation failed (probe, etc.) | 422 or 500 — NOT 200 |
| Service unhealthy | 503 |
| Unauthorized | 401 |

## 8. Filtering, Sorting & Pagination

All list endpoints MUST provide a consistent, frontend-friendly query contract.

### 8.1 Filtering

Every list endpoint MUST support **both** filter syntaxes for multi-value filtering:

| Syntax | Example | Behavior |
|--------|---------|----------|
| Comma-separated | `?status=INDEXED,FAILED` | Match any value in list |
| Bracket repeat | `?status[]=INDEXED&status[]=FAILED` | Same behavior, HTML-form friendly |
| Single value | `?status=INDEXED` | Exact match |

**Rules:**
- Filters apply as **AND** across different fields.
- Filters apply as **OR** within the same field.
- Filter values are **case-insensitive** unless documented otherwise.
- Unknown filter parameters MUST be silently ignored.

### 8.2 Sorting

All list endpoints MUST support `sort` parameter:

| Syntax | Example | Behavior |
|--------|---------|----------|
| Ascending (default) | `?sort=created_at` | Oldest first |
| Descending | `?sort=-created_at` | Newest first (prefix `-`) |
| Multi-field | `?sort=-created_at,name` | Primary desc, secondary asc |

### 8.3 Pagination

All list endpoints MUST support:

| Parameter | Type | Default | Constraints |
|-----------|------|---------|-------------|
| `limit` | int | 20 | `1 ≤ limit ≤ 100` |
| `offset` | int | 0 | `0 ≤ offset` |

Response MUST include pagination metadata in `meta`:

```json
{
    "success": true,
    "data": { "items": [...] },
    "meta": {
        "pagination": {
            "limit": 20,
            "offset": 0,
            "count": 142,
            "has_more": true
        },
        "request_id": "uuid"
    }
}
```

## 9. Forbidden Patterns

- **Raw storage reads in route handlers**: Use service abstractions — never raw DB calls directly in routes.
- **Inner-function imports in routes**: All imports at top of file.
- **Untyped error responses**: All error paths MUST return the response envelope with `success: false`.
