# Coding Standards — Generic

Use these rules when the project's language isn't one of the
specifically-supported set (currently Python, TypeScript, Go). They
cover universal practices. Augment with a project-specific
`coding-standards.md` at `<data_dir>/playbooks/<task>/` for anything
language-specific your team enforces.

## Naming

1. Names describe intent, not type. `userIds`, not `userIdList`.
2. Constants in UPPER_SNAKE_CASE; everything else follows the
   language's idiom.
3. Functions named with verbs (`fetchUser`, not `userFetcher`),
   classes / types with nouns (`UserRepository`, not `repositoryClass`).
4. Booleans read like questions: `isReady`, `hasPermission`, not
   `ready` (ambiguous) or `permission` (could be a permission object).

## Functions

5. One responsibility per function. If a function's name needs an
   `And`, split it.
6. Limit to ~50 lines or one screen, whichever comes first. Longer →
   extract helpers.
7. Pure functions (no side effects, deterministic) first; imperative
   shells around them. Easier to test, easier to reuse.
8. Document non-obvious return semantics or required preconditions
   in a docstring/doc-comment.

## Error handling

9. Fail fast at the boundary; propagate domain errors with context.
10. Don't swallow errors silently. Even logging+continue is a choice
    that needs documentation.
11. Make impossible states impossible via types where the language
    supports it (sum types, discriminated unions, NonEmptyList, etc.).

## State

12. Prefer immutability. Mutate in one place per scope at most.
13. Module-level mutable state requires a justification comment AND
    a way to reset it for tests.
14. Don't reach across abstraction layers to mutate; expose explicit
    setters/methods.

## Tests

15. Test the public contract, not the implementation. Refactor tests
    that break on internal restructuring.
16. Cover error paths, not just the happy path. Untested error paths
    are dead code in production.
17. Use the project's test framework; don't invent ad-hoc assertion
    helpers in test files.

## Style

18. Format with the project's auto-formatter. Don't hand-style.
19. Avoid clever one-liners that need 3 lines of explanation. Code
    is read 10× more than it's written.
20. Comments explain WHY, not WHAT. The code already says what.
