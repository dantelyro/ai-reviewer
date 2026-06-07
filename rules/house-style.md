<!--
House rules sent to Claude as part of every review (concatenated by
review.py from every *.md in this rules/ directory).

This is a STARTER file — replace the examples below with your team's actual
conventions and business rules. Keep each rule concrete and checkable;
vague guidance produces noisy reviews. Repo-specific rules can also live in
each Node repo's own CLAUDE.md, which Claude fetches via the MCP server.
-->

# House style & conventions (example — edit me)

- **No `console.log` in committed code.** Use the project logger. Flag any new
  `console.*` call outside of test files.
- **No `any` in TypeScript** unless accompanied by a `// eslint-disable` with a
  one-line justification. Prefer `unknown` + narrowing.
- **Components are function components with hooks.** Flag new class components.
- **No direct state mutation.** Flag in-place mutation of props or state
  objects/arrays (use immutable updates).
- **Effects must clean up.** Any `useEffect` that subscribes, adds a listener,
  or starts a timer must return a cleanup function.
- **User-facing strings go through i18n**, not hard-coded literals in JSX.

# Business rules (example — edit me)

- Replace this section with domain rules specific to your product so the
  reviewer can catch logic that violates them (e.g. validation invariants,
  permission checks, monetary rounding rules).
