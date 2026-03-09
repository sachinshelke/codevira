# Rule 009: Resilience and Observability

## 1. Fault Tolerance

- **Circuit Breakers**: Recommended for all external provider calls (LLM, databases, external APIs). Use your project's circuit breaker abstraction or a library like `tenacity`.
- **Retries**: Use exponential backoff for transient failures.
- **Timeouts**: Every external call MUST have a defined timeout.

## 2. Observability

- **Tracing**: Every request SHOULD have a trace/correlation ID propagated through the call chain.
- **Structured Logging**: Log events with context metadata (request_id, user_id where applicable) rather than raw strings.
- **Health Checks**: Every service MUST implement a `/health` endpoint returning current status.

## 3. Telemetry & Security

- **Metrics and Traces**: Export via OpenTelemetry-compatible sinks (OTEL, Prometheus, Datadog, etc.) when available.
- **Production logs MUST NOT contain PII**: Mask sensitive fields before logging.
- **API responses MUST NOT expose raw secrets**: Connection strings, API keys, and tokens MUST be masked before serialization. See Rule 019: API Standards § 2.
