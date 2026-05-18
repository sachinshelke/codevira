# Coding Standards — TypeScript

Use these rules when writing or modifying TypeScript / TSX files. Auto-
selected over `coding-standards.md` (the Python default) when the
project's detected language is `typescript`. Override per-project at
`<data_dir>/playbooks/<task>/coding-standards.md` (project-specific
content wins).

## Types and inference

1. Prefer **type inference** for locals and arrow-function returns;
   write explicit types for **exported APIs** and **public class members**.
2. Avoid `any`. Use `unknown` for values whose shape is genuinely
   unknown at the boundary, then narrow with a type-guard.
3. Use `interface` for object shapes that may be extended, `type` for
   unions, intersections, mapped types, or any non-extensible alias.
4. Mark optional properties with `?:` — do not use `| undefined` on
   the property type (changes the semantics in `strict` mode).
5. Enable `strict: true`, `noUncheckedIndexedAccess: true`, and
   `exactOptionalPropertyTypes: true` in tsconfig. New code must
   compile under all three.

## Modules and imports

6. Use **named exports**. Default exports collide on rename and break
   tree-shaking when re-exported.
7. Import via the package's documented entry (`import { foo } from
   "pkg"`), not deep paths into `dist/` — those are not part of the
   public API.
8. Group imports: external → internal → local, separated by blank
   lines. Sort within each group.

## Async and promises

9. Always `await` promises or explicitly attach `.catch()`. An
   unhandled rejection in Node will crash the process under
   `--unhandled-rejections=strict` (the v15+ default).
10. Use `async`/`await` over manual `.then()` chains. Top-level
    code uses `void someAsync()` if it doesn't care about result.
11. Run independent awaits in parallel: `await Promise.all([a(), b()])`.

## Errors

12. Throw `Error` subclasses (or a typed error class), never raw
    strings. Catchers can `instanceof` discriminate.
13. Don't swallow errors in production code. Log via the project's
    logger, then re-throw or wrap with context.

## Style

14. Use `===` / `!==`. Reserve `==` for explicit null-or-undefined
    checks where the looseness is intentional.
15. Destructure props in component / function signatures rather than
    `props.foo`. Improves grep-ability and locks the shape.
16. Use the project's lint config (eslint, biome, oxlint) as the
    formatting source of truth. Do not hand-style.
