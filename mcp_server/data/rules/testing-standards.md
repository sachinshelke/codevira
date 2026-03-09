# Rule 006: Testing Standards

## 1. Test Categories

| Category | Location | Purpose | I/O |
|----------|----------|---------|-----|
| Unit | `tests/unit/` | Isolated functions | None |
| Integration | `tests/integration/` | Cross-context flows | Real |
| Contract | `tests/contracts/` | Provider compliance | Mocked |
| E2E | `tests/e2e/` | Full system verify | Real |

## 2. Mandatory Contract Tests
- Every provider in `providers/` MUST pass its corresponding contract test in `tests/contracts/`.
- Providers implementing `AbstractLLMProvider`, `AbstractVectorStore`, etc., must be verified against identical test suites.

## 3. Test Naming & Structure

- **Naming Pattern**: `test_<action>_<condition>_<expected_result>`.
- **Example**: `test_search_with_empty_query_returns_empty_list()`.

## 4. Verification Policy
- "If it'\''s not tested, it doesn'\''t exist."
- All new features MUST include at least unit tests.
- Infrastructure changes MUST include contract tests.