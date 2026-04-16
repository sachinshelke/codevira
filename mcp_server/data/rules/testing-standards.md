# Rule 006: Testing Standards

## 1. Test Categories

| Category | Location | Purpose | I/O |
|----------|----------|---------|-----|
| Unit | `tests/` | Isolated functions, 1:1 per source file | None |
| Integration | `tests/integration/` | Cross-module flows | Real |
| E2E | `tests/e2e/` | Full system verify | Real |

## 2. Test Coverage Requirements
- Every new MCP tool MUST have corresponding unit tests.
- Every new module MUST have a matching `tests/test_<module>.py` file.

## 3. Test Naming & Structure

- **Naming Pattern**: `test_<action>_<condition>_<expected_result>`.
- **Example**: `test_search_with_empty_query_returns_empty_list()`.

## 4. Verification Policy
- "If it'\''s not tested, it doesn'\''t exist."
- All new features MUST include at least unit tests.
- Infrastructure changes MUST include contract tests.