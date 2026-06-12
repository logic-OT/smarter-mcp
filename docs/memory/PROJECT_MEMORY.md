# Project Memory - smarter-mcp

## Friday, 12-06-2026, 11:42 am, [main] Principal addendum appended to production assessment

- Added a Codex RTK addendum to `docs/reviews/2026-06-12-production-assessment.md`.
- New validated gaps: `smarter-mcp test` bypasses production wrappers, `/schema` leaks `expose: false` tools, public `build()` can bypass custom auth middleware, FastMCP dependency is unbounded despite deep imports, and MIT metadata lacks a repo license file.
- Keep remediation order focused on wrapper correctness, sanitized errors, real MCP test harness, schema from routed surface, auth fail-closed entrypoints, then CI/publish gates.
