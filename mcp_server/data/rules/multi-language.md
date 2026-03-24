# Multi-Language Project Rules

## TypeScript / TSX
- When adding a new module, re-export it from the barrel `index.ts`.
- Prefer `export function` over `export default` for named symbols.
- Use JSDoc `/** */` comments for public APIs — they are extracted by the indexer.
- When adding React components, co-locate styles and tests in the same directory.

## Go
- Exported symbols start with an uppercase letter. Name unexported helpers with lowercase.
- When adding a new HTTP handler, register it in the router (e.g., `routes.go`).
- Follow the `func (s *Server) handleX(w http.ResponseWriter, r *http.Request)` pattern.
- Add a `// Package <name>` doc comment at the top of each package's primary file.

## Rust
- When adding a new module, declare it in `mod.rs` or `lib.rs` with `pub mod <name>;`.
- Use `///` doc comments for public APIs — they are extracted by the indexer.
- Prefer `pub fn` for the public API surface; keep internals private by default.
- When adding a trait, provide a default implementation where sensible.
