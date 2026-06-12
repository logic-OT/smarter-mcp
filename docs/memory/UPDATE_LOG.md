# Update Log — smarter-mcp

## Friday, 12-06-2026, 11:42 am, [main] Principal addendum appended to production assessment

Codex RTK follow-up added a non-duplicative section to `docs/reviews/2026-06-12-production-assessment.md`: validates Fable 5's verdict, sharpens the false-confidence risk in `smarter-mcp test`, and adds schema leak, public auth-entrypoint split, FastMCP compatibility, and missing license-file gaps.

## Friday, 12-06-2026, 11:22 am, [main] Full production assessment written to docs/reviews/

Five-dimension parallel review (security, correctness, robustness, DX/docs/CI, performance) of the whole codebase. 5 Critical findings, all reproduced by execution: str-returning tools broken (image coercion), session lifecycle inert (Context never injected), `SmarterMCP("name")` positional-arg trap, `discover_module` broken for packages/classes, extraction errors silently discarded. See `docs/reviews/2026-06-12-production-assessment.md` for the full report and ordered remediation roadmap. Ground truth: 53 tests pass (extractor-only coverage), 112 ruff errors, no CI, `/tests` gitignored.
