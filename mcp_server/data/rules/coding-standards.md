---
trigger: always_on
---

# Rule 005: Coding Standards

## 0. Python Standardization
All Python code MUST follow the standardization and best practices defined in the **Python Enhancement Proposals (PEPs)** as documented at [peps.python.org](https://peps.python.org). Specifically, adherence to **PEP 8** (Style Guide) is mandatory.

## 1. File Size Limits

| File Type | Max Lines |
|-----------|-----------|
| Service | 300 |
| Entity | 150 |
| Command Handler | 100 |
| Test | 200 |
| Any file | 500 (absolute max) |

## 2. Mandatory Language Features

### 2.1 Type Hints
- Mandatory on all signatures (args and return types).
- Use specific types, avoid `Any` where possible.

### 2.2 Docstrings
- Required for all public functions/methods.
- Must include Args, Returns, and Raises sections.

## 3. Error Handling

- **Bare except is FORBIDDEN**: Always catch specific exceptions.
- **Structured Error Return**: Use consistent error return patterns appropriate for your architecture (e.g., Result types, typed exceptions, or error objects).

## 4. Logging & Telemetry

- **Structured Logging**: Use your project's logging abstraction — avoid raw `print()` in production code.
- **Context Binding**: Ensure request/trace identifiers are present in logs for correlation.
- **Structured Info**: Pass keyword arguments to logger instead of formatted strings.

## 5. Generic Engine Principle
- **Zero Hardcoding**: Magic numbers, thresholds, and project-specific constants MUST live in configuration files, not in code.
- **Behavior via Policy**: Changes to system behavior should be achieved via configuration/policy, not code modification.

## 6. Module Structure & Imports (PEP 8)
- **Top-Level Imports**: All imports MUST be placed at the top of the file.
- **Inner-function Imports**: Strictly FORBIDDEN unless used to resolve a *circular dependency* or for *heavy lazy-loading* in a CLI subcommand where performance is critical.
- **Grouping**: Group imports into standard library, third-party, and local modules, separated by a blank line.
