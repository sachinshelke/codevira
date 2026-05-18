# Coding Standards — Go

Use these rules when writing or modifying Go (`.go`) files. Auto-
selected over `coding-standards.md` when the project's detected
language is `go`. Override per-project at
`<data_dir>/playbooks/<task>/coding-standards.md` for any project-
specific tweaks.

## Naming

1. Exported identifiers start with an uppercase letter; unexported
   start lowercase. Don't use `_` to make a package member private —
   case is the contract.
2. Package names are short, lowercase, no underscores (`net/http`,
   not `net_http`). The import path's last segment IS the package
   name in idiomatic code.
3. Receiver names: 1-2 characters, consistent across the type's
   methods. Don't use `self` or `this`.
4. Interface names ending in `-er` describe the single method (e.g.
   `Reader` for `Read`). Multi-method interfaces get domain names.

## Errors

5. Return errors as the LAST return value. Never panic across
   package boundaries — panic is for unrecoverable invariant
   violations only.
6. Wrap errors with context using `fmt.Errorf("loading config: %w",
   err)`. The `%w` verb preserves the chain for `errors.Is` /
   `errors.As`.
7. Check every error explicitly. `_ = someCall()` requires a comment
   explaining why the error is safe to ignore.
8. Don't shadow `err` in nested scopes — produces silent loss of
   information. Use a different name (`innerErr`) when nesting.

## Concurrency

9. Document the goroutine lifecycle when you launch one — who stops
   it, on what signal. Goroutines without a stop story leak.
10. Prefer `context.Context` for cancellation propagation. Pass it
    as the first parameter (`func DoThing(ctx context.Context, ...)`).
11. Don't pass mutexes by value; embed them in structs that hold the
    protected state, and document what they protect.
12. Use channels for ownership transfer; use mutexes for shared
    state. Mixing the two patterns produces subtle deadlocks.

## Build and modules

13. Run `gofmt` (or `goimports`) before committing. CI rejects any
    deviation.
14. Run `go vet ./...` on every PR. Fix the warnings; don't add
    `//nolint` without a one-line justification.
15. Pin module versions via `go.mod`. Don't use `replace` directives
    against forks unless documented in CHANGELOG.

## Tests

16. Test files are `*_test.go` in the SAME package (`foo.go` →
    `foo_test.go` in the same package, or `foo_external_test.go` in
    `package foo_test` for black-box tests).
17. Use table-driven tests (`tests := []struct{ ... }{...}`) when
    you have ≥3 cases of the same shape.
18. Sub-tests via `t.Run("description", func(t *testing.T) {...})`
    so failures point at the specific case.
