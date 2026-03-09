# Rule 013: Smoke Testing & Edge Case Hardening

## Objective
To ensure the system remains robust under extreme conditions, invalid inputs, and infrastructure failures. All major features MUST be accompanied by a comprehensive "Smoke Test" suite that covers positive, negative, and boundary scenarios.

## 1. The Smoke Test Manifesto
Smoke tests are NOT unit tests. They verify end-to-end system health and resilience.
- **Fail Fast**: If a smoke test fails, the build/deployment MUST abort.
- **Infrastructure Aware**: Tests must gracefully handle simulation vs. production providers.
- **State Neutral**: Tests must clean up after themselves or use isolated namespaces.

## 2. Mandatory Edge Case Coverage
Every Smoke Test suite MUST include the following scenarios:

### A. Input Extremes
- **Empty/Whitespace**: "", "   ", "\n"
- **Massive Input**: 100KB+ queries or payloads.
- **Unicode/Multilingual**: Emoji, non-Latin scripts (CJK, Cyrillic, Hindi).
- **Special Characters**: SQL injection characters (', ", ;, --), control characters.

### B. Parameter Boundaries
- **Limits**: limit=0, limit=-1, limit=999999999
- **Thresholds**: Below 0.0, above 1.0, exactly 0 or 1.
- **Timeframes**: Distant past (1970), far future (2099), current second.

### C. Infrastructure Resilience (Chaos)
- **Closed Circuit**: Normal operation.
- **Open Circuit**: System must return InfrastructureError or graceful fallback, NOT hang.
- **Retry Success**: System must succeed on 2nd or 3rd attempt if initial call fails.
- **Latency**: System must handle slow response times (timeouts).

### D. Idempotency & Concurrency
- **Double-Run**: Executing the same command twice must produce identical results/state.
- **Race conditions**: Concurrent writes to the same entity/pattern must be protected by locks.

## 3. Implementation Standard
1. **Verification Scripts**: Use scripts/verify_<feature>.py.
2. **Rich Feedback**: Use core.logging and rich for clear PASS/FAIL reporting.
3. **Exit Codes**: Return non-zero exit code on any failure.

## 5. Verification Etiquette & Safety

- **Non-Destructive by Default**: Smoke tests MUST NOT perform destructive actions (like `shutdown`) as part of a shared suite (`verify_all.py`) unless explicitly marked as a "Final/Clean-up" step.
- **Reentrant-Proof**: If a test creates data, it should use a unique `id` or cleanup after itself to avoid failing on a second run.
- **Authentication Resilience**: 
    - Health checks should remain public.
    - Data APIs should require auth but the test suite MUST handle missing secrets gracefully (warn/skip rather than fail) to allow for basic health verification.
- **Connectivity Check**: Before running complex API tests, the suite MUST verify that the target service is actually online.
