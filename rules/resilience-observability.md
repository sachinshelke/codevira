# Rule 009: Resilience and Observability

## 1. Fault Tolerance

- **Retries**: Use exponential backoff for transient failures (git subprocess calls, file I/O).
- **Timeouts**: Every subprocess call MUST have a defined timeout (e.g., `timeout=3` for git commands).
- **Graceful Degradation**: When optional dependencies are missing (chromadb, tree-sitter), tools MUST continue with reduced functionality rather than crashing.

## 2. Observability

- **Structured Logging**: Use Python `logging` module with context metadata rather than raw print statements.
- **Crash Logging**: All unhandled exceptions are captured to `~/.codevira/logs/crashes.log` with automatic sanitization of sensitive data (connection strings, passwords, private IPs).

## 3. Security

- **Crash logs MUST NOT contain PII**: The crash logger sanitizes connection strings, key=value secrets, and home directory paths before writing to disk.
- **MCP tool responses MUST NOT expose raw secrets**: Connection strings, API keys, and tokens MUST be masked before returning to the AI agent.
