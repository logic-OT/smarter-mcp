# Update Log — smarter-mcp

## Friday, 12-06-2026, 4:57 pm, [feat/discovery-api] PR4 — discovery + API contract fixes
Resolved C3 (ctor arg order: `name` first, `source_root` kw-only, nonexistent path raises, `find_manifest` bounded at VCS boundary), C4 (`discover_module` works for packages + classes, invocable end-to-end), C5 (extraction errors surfaced in discover/validate/health, `validate` exits non-zero), H12 (decorator tools served unprefixed; full dotted-path namespaces; collision warnings), H13 (`@property` resources bound via InstanceManager), H14 (manifest source paths resolved relative to manifest dir), M6 (import-failure ERROR + aggregate summary, exposed in /health), M8 (`extraction_result` populated). Two-stage review caught a tuple-unpack crash in `init`, a `rstrip(".py")` name-mangling bug, in-place ExtractionResult cache mutation, and an H13 test that didn't test the fix — all fixed. 153 tests pass; harness C3/C4a/C4b/C5 NOT REPRODUCED.

## Friday, 12-06-2026, 2:30 pm, [feat/schema-coercion] PR3 — schema + coercion correctness (merged #7)
Single shared `_typeparse.py` normalizer now drives both schema generation and coercion. H11 (Optional/Union/List/Dict/Literal map to correct JSON Schema with anyOf/items/enum), H9 (no RecursionError on `list[int | None]`), strict bool/int coercion (reject unrecognized bool strings, non-integral/non-finite floats), M19 (1 MiB json.loads guard), M3 (non-literal defaults use a sentinel), element-wise coercion so `list[int]` input matches the published schema. 119 tests pass; harness H9/H11 NOT REPRODUCED.

## Friday, 12-06-2026, 1:00 pm, [feat/runtime-core] PR2 — runtime core repair (merged #6)
C1 (str/bytes/Path returns no longer coerced into Image), C2 (session lifecycle keeps one instance per session via `get_context()`; bounded LRU eviction with best-effort close outside the lock), H10 (static/classmethods route correctly), H20 (thread-safe singleton/session creation), M5 (Context injected by actual param name incl. Optional/union), M4 (`build()` idempotent). 65 tests pass; harness C1/C2 NOT REPRODUCED.

## Friday, 12-06-2026, 11:42 am, [main] Principal addendum appended to production assessment

Codex RTK follow-up added a non-duplicative section to `docs/reviews/2026-06-12-production-assessment.md`: validates Fable 5's verdict, sharpens the false-confidence risk in `smarter-mcp test`, and adds schema leak, public auth-entrypoint split, FastMCP compatibility, and missing license-file gaps.

## Friday, 12-06-2026, 11:22 am, [main] Full production assessment written to docs/reviews/

Five-dimension parallel review (security, correctness, robustness, DX/docs/CI, performance) of the whole codebase. 5 Critical findings, all reproduced by execution: str-returning tools broken (image coercion), session lifecycle inert (Context never injected), `SmarterMCP("name")` positional-arg trap, `discover_module` broken for packages/classes, extraction errors silently discarded. See `docs/reviews/2026-06-12-production-assessment.md` for the full report and ordered remediation roadmap. Ground truth: 53 tests pass (extractor-only coverage), 112 ruff errors, no CI, `/tests` gitignored.
