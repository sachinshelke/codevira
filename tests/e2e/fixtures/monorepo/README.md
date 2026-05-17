# Monorepo Fixture

A `packages/<name>/src/` layout typical of pnpm/Yarn/Turborepo
workspaces. Tests that codevira's directory detection handles
nested package structures correctly — does it find code in
`packages/foo/src/`, or does it stop at `packages/`?
