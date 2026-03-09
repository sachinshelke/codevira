---
trigger: always_on
---

# Rule 011: Engineering Excellence & System Integrity

## 1. Logic Over Legacy
- **Principle**: Existing code is a **Requirements Reference**, not a technical template.
- **Mandate**: Principal AI Systems Architect /Principal/Staff AI level re-evaluation is required for every module. Logic MUST be optimized for performance, scalability, and the Five Laws during re-implementation.
- **No Blind Porting**: If legacy logic is inefficient, insecure, or poorly decoupled, it MUST be redesigned from first principles.

## 2. Interface Discipline (CLI & API)
- **Principle**: Minimalism is a Feature.
- **Mandate**: Every CLI command MUST be audited before re-implementation. If a command is redundant, confusing, or too low-level, it MUST be combined, renamed, or eliminated.
- **UX Standard**: CLI feedback must be actionable, rich (using `rich` library), and provide clear next-steps guideance.

## 3. Idempotency & Replay Safety
- **Mandate**: All processing logic (ingest, warehouse, reasoning) MUST be idempotent. Re-running the same operation with the same input MUST result in the same state without side effects or duplicates.

## 4. High-Precision Concurrency & Performance
- **Asynchronous First**: Use `asyncio` correctly. Avoid blocking the event loop.
- **Zero-Waste Execution**: Optimize for minimal LLM tokens and DB roundtrips.

## 5. The Principal Gate
- **Design Blueprints**: Before re-implementing a major context, a "Logic Blueprint" MUST be proposed.

## 6. Security by Design
- **Secure Credentials**: Never allow raw secrets to breathe in logs. Use the root `.env` file for secrets and ensure it is NEVER committed to version control.