# Finding validation harness

End-to-end reproduction of the findings in
[`docs/reviews/2026-06-12-production-assessment.md`](../../docs/reviews/2026-06-12-production-assessment.md).

Each check builds a **real** `SmarterMCP` server and, where relevant, calls tools
through FastMCP's in-memory `Client` transport — the same path a real MCP client
uses. `CONFIRMED` means the reported bug reproduced; `NOT REPRODUCED` means the
code behaved correctly.

## Run

```bash
bash tests/validation/run_validation.sh
```

All stdout+stderr is captured to `logs/validation-<timestamp>.log` (and symlinked
to `logs/validation-latest.log`). The harness exits 0 regardless of findings —
it is a diagnostic, not a gate.

## Result (commit at time of writing)

10/10 findings CONFIRMED, 0 false positives:

| ID | Finding | Evidence |
|----|---------|----------|
| C1 | str/Path/bytes returns coerced to `Image` | `greet("Ada")` → `FileNotFoundError: 'Hello, Ada!'` from FastMCP's image serializer |
| C2 | `session` lifecycle degrades to per-call | two `bump()` calls in one session both return `1`; `No context provided ... falling back to per-call` logged twice |
| C3 | `SmarterMCP("my-server")` parses name as path | `config.name='my-mcp-server'`, phantom `SourceConfig(path='my-server')` |
| C4a | `discover_module(package)` registers 0 tools | `discover_module(json, include=['dumps','loads'])` → 0 tools |
| C4b | `discover_module(class)` yields unbound, schema-less tools | `class_name=None`, 0 params |
| C5 | extraction errors silently discarded | dir with a syntax-error file → only the valid tool survives, `extraction_result=None`, no error raised |
| H9 | nested-union coercion recurses infinitely | `list[int \| None]` → `RecursionError` |
| H11 | `Optional`/`List`/`Literal` map to `"string"` | published schema: `a,b,c → 'string'`, plain `int d → 'integer'` |
| H15 | manifest silently ignores unknown keys | bogus `private`/`unannotated`/`totally_made_up_key` load clean |
| H1 | insecure defaults | `host='0.0.0.0'`, `auth_enabled=False`, `rate_limit_enabled=False` |
