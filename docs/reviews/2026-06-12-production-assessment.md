# Production Assessment — smarter-mcp

**Friday, 12-06-2026, 11:22 am, [main] Full-codebase production review**

> *A fable: A miller built a machine that could turn any grain into bread. He polished the hopper until it gleamed, wrote songs about the loaves, and sold tickets to the demonstration. On opening day the villagers poured in their wheat — and the machine, which had never once been fed real grain end-to-end, ground their flour into the gears. The hopper was genuinely excellent. Nobody had ever tasted the bread.*
>
> **Moral: test the loaf, not the hopper.** The extraction layer here is well-engineered and well-tested; the runtime layer that actually serves tools has zero tests and breaks the README's own three flagship examples.

---

## Scope and method

- **Target:** entire repo at commit `c36413a` (`version_1.1`), package `src/smarter_mcp` (~6,100 LOC src, ~755 LOC tests), docs, packaging, CI.
- **Method:** five parallel specialist reviews (security · correctness/code-quality · robustness/silent-failures · DX/docs/packaging/CI · performance), cross-checked and deduplicated. **All Critical findings were reproduced by executing the code** against the installed FastMCP 3.3.1 — they are not pattern-matched speculation.
- **Ground truth at review time:** `pytest` → 53 passed in 1.75s. `ruff check src/ tests/` → **112 errors** (72 auto-fixable). No CI exists to run either.

## Verdict

**Not production-ready. Alpha-accurate, README-inaccurate.** The pyproject classifier (`Development Status :: 3 - Alpha`) is honest; the README ("production-grade", "Cleaned up on session end", "No silent name collisions") is not. The dual-pass extractor, manifest layer, and rate limiter would survive senior review. The runtime layer (`tool_wrapper.py` + `multimodal/interceptor.py` + instance lifecycle) would not survive any review: a tool that returns a string fails every call, session state never works, and `discover_module` on a package registers zero tools. The test suite covers ~4 of 25 modules — none of the runtime/server path — which is exactly why all of this ships green.

## Scorecard

| Dimension | Grade | One-line summary |
|---|---|---|
| Correctness | **F** | str-returning tools, session lifecycle, `discover_module(package)`, static/classmethods all broken (verified by execution) |
| Security | **D** | `0.0.0.0` + auth off by default, SSRF in image path, tracebacks to clients, pickle cache, timing-unsafe key compare |
| Robustness | **D** | Extraction errors silently discarded; `validate` says "✓" over syntax errors; no timeouts on LLM/URL fetches |
| Efficiency | **C** | Hot path does per-call re-derivation; async wrappers block the event loop; serial LLM enrichment stalls startup |
| Test coverage | **F** | 53 good extractor tests; zero for runtime, server, security, CLI, config, LLM. `/tests` is gitignored — tests have already been lost |
| CI/CD & supply chain | **F** | No CI at all; publish on branch-name push, untested, long-lived PyPI token, unpinned actions |
| Docs accuracy | **D** | Every `SmarterMCP("name")` example misuses the API; README documents manifest keys that don't exist; killer-feature example registers 0 tools |
| DX (API/CLI/typing) | **C+** | Decorator ergonomics and CLI niceties are genuinely good; no `py.typed`, no `--version`, `-h` trap, dead config keys |
| Architecture & code quality | **B-** | Clean layering, good docstrings, well-designed extractor IR; runtime layer ignores metadata the extractor correctly produces |

---

## Critical — all verified by execution

### C1. Every tool returning `str`, `bytes`, or `Path` fails — including the README's flagship `greet`
`src/smarter_mcp/runtime/tool_wrapper.py:98,115,163,181` → `src/smarter_mcp/multimodal/interceptor.py:119-123`

All four wrapper variants unconditionally pipe results through `coerce_to_fastmcp_image()`, whose first branch is `isinstance(val, (Path, str)) → Image(path=str(val))`. Reproduced end-to-end: `greet("Ada")` → `ToolError: [Errno 2] No such file or directory: 'Hello, Ada!'`. `bytes` results are silently mislabeled as PNG images. The single most common return type is broken framework-wide.

**Fix:** coerce only when the tool's return annotation (available at wrap time in `tool.extracted_obj.return_type`) is image-like, or the runtime value is `PIL.Image.Image` / `np.ndarray` / `fastmcp.Image`. Never treat `str`/`Path`/`bytes` as images. Gate on `multimodal.auto_detect` (currently dead config).

### C2. Session lifecycle never works — `@toolkit(lifecycle="session")` silently degrades to per-call, and nothing is ever cleaned up
`src/smarter_mcp/runtime/tool_wrapper.py:54-72`, `src/smarter_mcp/runtime/instances.py:98-116`

`build_tool_wrapper` forges `wrapper.__signature__` from the impl's parameters, omitting the wrapper's `ctx: Context` param. FastMCP injects Context based on the advertised signature, so `ctx` is always `None` and every call hits the per-call fallback with a WARNING. Reproduced: two `bump()` calls in one session both return `1`. Consequences stack: stateful toolkits (DB connections, loaded models) are reconstructed per call; `_session_instances` is never evicted on disconnect (unbounded growth); **no cleanup path exists anywhere** — no `close()`/`__exit__` is ever called for session, singleton, or per-call instances. README's "One instance per session... Cleaned up on session end" is doubly false.

**Fix:** keep a Context-annotated keyword-only `ctx` in the forged signature (FastMCP hides Context params from schemas), or use `get_context()` inside the wrapper. Hook session close to evict and best-effort-close instances (per-instance try/except). Add TTL/LRU backstop.

### C3. `SmarterMCP("my-server")` treats the name as a filesystem path — every constructor example in README/docs/spec is wrong
`src/smarter_mcp/server/app.py:181-204,236-241`, `src/smarter_mcp/config/manifest.py:320-346`

First positional param is `source_root`; `name` is keyword-only. FastMCP convention and every example in this repo's own docs pass the name positionally. Reproduced: `SmarterMCP('my-server')` yields server name `my-mcp-server`, a phantom source entry, and a clean `build()` with zero tools (os.walk on a missing dir yields nothing, no error). Worse: `find_manifest()` walks parents to the filesystem root, so a stray `smarter-mcp.yaml` anywhere above CWD silently hijacks config — **this actually happens inside this repo** via the leftover root manifest (see H10).

**Fix:** make the first positional `name` (matching FastMCP and the docs), `source_root` keyword-only; raise on nonexistent `source_root`; bound `find_manifest` to the project root.

### C4. `discover_module()` is broken for packages (zero tools) and classes (unbound, schema-less tools)
`src/smarter_mcp/extractor/surface.py:737-754`, `src/smarter_mcp/server/app.py:374-435`

For a package, module-name derivation reduces to `"__init__"`, so impl resolution fails ("attempted relative import with no known parent package") and only `__init__.py` is even extracted — reproduced: `discover_module(json, include=["dumps","loads"])` (the docs' killer-feature example) registers **zero tools** (compounded by the variadic-skip policy eating `*args` functions even when explicitly included). For a class (README's `discover_module(pd.DataFrame, ...)`), the inspect-only fallback registers unbound methods with `class_name=None` and **empty parameter schemas** — reproduced: `('describe', None, [])`.

**Fix:** for packages, set `source_root` to the package's parent and walk submodules via `extract()`; detect `inspect.isclass()` and route through the toolkit/instance machinery; build fallback params from `inspect.signature`; let explicit `include=` override the variadic skip.

### C5. Extraction errors are collected and thrown away — tools vanish silently, `validate` lies
`src/smarter_mcp/extractor/surface.py:651-661`, `src/smarter_mcp/server/app.py:339-347`, `src/smarter_mcp/cli/main.py:144-217`

`ExtractionResult.errors/warnings` (syntax errors, undecodable files, read failures via the broad `except Exception`) are populated and never read by `discover()` or the CLI. Reproduced: a `broken.py` with a syntax error produces zero output at default log level; the server starts with that module's tools missing; `smarter-mcp validate` prints "✓ Validation successful!" over the same tree.

**Fix:** log `errors` at ERROR and `warnings` at WARNING in `discover()`; print both in `validate` and exit non-zero on errors; expose counts in `/health`; offer `strict` mode aborting startup.

---

## High

### Security

**H1. Insecure defaults: `0.0.0.0` bind + auth off + rate limiting off** — `config/manifest.py:59,66`; `cli/main.py:401` scaffolds `host: "0.0.0.0"` into every generated manifest. A default `smarter-mcp serve ./mylib` exposes every discovered tool to the network unauthenticated. *Fix:* default `127.0.0.1`; loud startup warning when binding non-loopback with auth disabled.

**H2. SSRF via image-parameter URL fetch** — `multimodal/interceptor.py:69-72`. Any string sent for a PIL/ndarray-typed param triggers `urllib.request.urlopen(val)` — no allowlist, no timeout, no redirect cap. Cloud metadata (`169.254.169.254`) and internal services are reachable; local file paths are also readable via the `Path(val).is_file()` branch (client-controlled file disclosure). For async tools this blocking fetch runs **on the event loop**, stalling all sessions. *Fix:* opt-in only; block private/link-local/loopback ranges; timeout + size cap; thread offload.

**H3. No image decompression-bomb limits; `image_max_size` is dead config** — `interceptor.py:71,100,107`; `manifest.py:185`. Unbounded `read()`/`b64decode`/`PIL.Image.open` with default `MAX_IMAGE_PIXELS`. *Fix:* byte ceiling, pixel cap, actually apply the configured max size.

**H4. Full server tracebacks returned to MCP clients; `isError` never set** — `errors.py:45-54` + all wrappers. Leaks file paths, stack frames, potentially secrets in exception messages; agents can't even distinguish failure from success (error payloads return as successful text). Directly contradicts docs/README.md:572 ("the agent only ever sees the clean structured object"). *Fix:* raise `fastmcp.exceptions.ToolError` with a sanitized message; tracebacks to server logs only, behind a debug flag.

**H5. Pickle extraction cache = local code-execution vector, anchored to CWD** — `cache.py:35,88`. `pickle.loads` on `.smarter-mcp/extraction-cache/*.pkl` under whatever directory the server starts in; `--dev` auto-enables it. Anyone who can write that dir (shared runner, repo shipping a poisoned `.smarter-mcp/`) executes code at next start. *Fix:* JSON-safe serialization of the IR (the enum/MISSING blockers are trivially encodable), anchor to `source_root`, document the trust boundary.

**H6. Supply chain: publish pipeline is untested, token-based, branch-triggered, unpinned** — `.github/workflows/publish.yml`. Publishes on push to any `version_*` branch (already drifted: branch `version_1.1` shipped version `0.1.1`), runs zero tests first, uses long-lived `PYPI_TOKEN` instead of Trusted Publishing (OIDC), actions pinned to mutable tags (`setup-python@v4` is also outdated). *Fix:* tag-driven release gated on CI, `id-token: write` trusted publishing, SHA-pinned actions, least-privilege `permissions:`.

**H7. Timing-unsafe API key comparison** — `security.py:72` (`provided not in self.valid_keys`). *Fix:* `hmac.compare_digest` against each key, accumulate result. Related inconsistency: with `auth_enabled=True` and no keys set, the custom middleware fails **closed** (401 everything) while the FastMCP bearer verifier fails **open** with only a warning (`security.py:89-94` vs `app.py:683-696`) — pick one; failing startup loudly is better.

**H8. `/health` always unauthenticated and leaks namespaces/counts/version; `/schema` errors return HTTP 200** — `health.py:36-43`, `security.py:37`, `app.py:589-597`. Health is also hardcoded `"healthy"` and counts tools that failed registration. *Fix:* bare status for unauthenticated callers; 404 for missing namespaces; degraded status wired to extraction/registration failures.

### Correctness

**H9. RecursionError coercing any generic containing a nested union** — `coercion.py:98-111`. `list[int | None]` has `"|" in s` true but splits to one part → infinite recursion. Reproduced. *Fix:* `parts = _split_top_level(s, "|"); return parts if len(parts) > 1 else None`.

**H10. Static methods and classmethods are unusable as tools** — `tool_wrapper.py:57-61,160-181`. Any tool with `class_name` set gets its first real param dropped from the schema and an instance injected as first arg. Reproduced: `ToolError ... Unexpected keyword argument`. The extractor correctly tags `CallableKind.STATICMETHOD/CLASSMETHOD`; the wrapper ignores it. *Fix:* branch on kind.

**H11. JSON schema generation maps `Optional`, `Union`, `List`, `Literal` to `"string"`** — `_schema.py:73-79,135-141`. Only lowercase builtins map; arrays never get `items`; the advertised schema disagrees with runtime coercion (which handles Optional correctly). Compounding: the inspect pass *degrades* generics the AST got right (`list[int]` → `list`, `surface.py:396-404`). *Fix:* proper type-string parsing (anyOf/enum/items), prefer the richer AST string, and share one normalizer with `coercion.py` (currently duplicated four ways).

**H12. Naming contract drift and silent collisions** — `router.py:137-141`, `_registry.py:85,184-187`. Every decorator tool serves as `default_<name>` (explicit `@tool(name="bump")` on a toolkit becomes `default_DB_bump`); namespace is the *last* module segment so `a/utils.py` and `b/utils.py` collide wholesale, last-write-wins, no log — directly contradicting README's "No silent name collisions". The router's correct `_module_to_namespace` is dead code. *Fix:* mount `default` without prefix, don't class-prefix explicit names, derive namespaces from full dotted paths, warn on overwrite.

**H13. Discovered `@property` resources never register** — `router.py:212-227` registers the unbound `fget`; FastMCP rejects `self`-first functions; failure reduced to a warning. `include_properties: true` is effectively dead. *Fix:* bind through `InstanceManager` before registering.

### Robustness / Process

**H14. Manifest source paths resolve against CWD, not the manifest** — `manifest.py:79-81` (docstring promises manifest-relative) vs `app.py:534-535`. `smarter-mcp serve -m /elsewhere/smarter-mcp.yaml` from another dir scans the wrong/nonexistent path and starts cleanly with 0 tools. *Fix:* resolve against the manifest's directory; error on nonexistent/empty `source_root`.

**H15. Manifest silently ignores unknown keys — and the README documents keys that don't exist** — no `extra="forbid"` on any config model. README's `expose: {private:, unannotated:}` and `llm: {cache: true}` blocks are entirely inert (real keys: `include_private`, `unannotated_policy`, `cache_path`; the README LLM block also omits `enabled: true` so it does nothing). Dead parsed config compounds it: the **entire `multimodal` block**, `server.cors_origins`, `server.log_level`, `routing.base_path`, `routing.root_aggregate`, `ToolOverride.param_descriptions`, and `SourceConfig.include` for `path:` sources are read nowhere (grep-verified). *Fix:* `extra="forbid"` everywhere, wire up or delete dead fields, correct the README.

**H16. LLM enrichment: serial, blocking, no sane timeout, per-tool failure spam** — `llm/client.py:79-91`, `generator.py:148-166`. One blocking chat call per undescribed tool during `build()` with SDK defaults (600s timeout); 100 tools ≈ minutes of cold start; a bad key yields one failed round-trip + WARNING per tool, then ships empty descriptions; malformed output is cached forever. And then `router.py:70-77` truncates **every** description — including paid-for LLM ones and explicit `@tool("...")` strings — to the first line. *Fix:* 10–30s timeout, abort enrichment on auth-class errors, batch or bound-concurrency, sanitize before caching, stop truncating explicit descriptions.

**H17. No CI; lint failing; `/tests` is gitignored** — `.gitignore:33` ignores `/tests`; tracked tests survive only by predating the ignore, and tests have **already been lost** this way (the leftover root manifest references a CLI scaffolding test that no longer exists — `cli/`, `server/`, `runtime/` have zero tests today). 112 ruff errors with no enforcement. *Fix:* delete the ignore line, add `ci.yml` (py3.10–3.13 matrix: ruff + pytest), gate publish on it.

**H18. Root `smarter-mcp.yaml` (948 lines) is leftover test cruft that actively hijacks the framework** — `name: "pymcp"`, source path pointing at a macOS pytest tmpdir from another machine, ~900 commented stubs of smarter-mcp's own internals. Because `find_manifest` walks parents (C3), any `SmarterMCP()` run inside this repo silently loads it. *Fix:* delete; put a curated example under `examples/`.

**H19. Dependency hygiene** — `pyproject.toml:27-35`. `openai>=1.0` is mandatory for an opt-in feature whose lazy-import machinery already supports being an extra — and docs/README.md:703 instructs `pip install smarter-mcp[llm]`, **an extra that does not exist**. `structlog` and `jinja2` are declared and never imported anywhere in src/ (the "structured logging" implication is false — everything uses stdlib `logging`). *Fix:* create the `[llm]` extra, drop the dead deps.

**H20. `InstanceManager` check-then-set races** — `instances.py:81-83,112-116`. FastMCP 3.x runs sync tools in a threadpool (verified in installed package); unlocked singleton creation can construct two DB pools and leak one. *Fix:* lock around creation (the rate limiter at `security.py:159` already models the right pattern).

---

## Medium

| # | Finding | Where | Fix direction |
|---|---|---|---|
| M1 | Bool coercion maps `"banana"`/`"2"` silently to `False`; int coercion truncates `3.7`→`3` | `coercion.py:150-159` | raise `CoercionError` outside known true/false sets; reject non-integral floats |
| M2 | Image-resolution failure silently passes the raw string to the user's function → confusing downstream TypeError | `coercion.py:182-187` | raise `CoercionError` |
| M3 | Non-literal defaults stored as unparse strings (`"datetime.now()"`) → published as schema defaults, inferred type `str` | `surface.py:101-107`, `type_inference.py:39-56`, `_schema.py:95-96` | NON_LITERAL sentinel |
| M4 | `build()` not idempotent — re-extends `tool.tests` with duplicate cases each call | `app.py:525-546` | `_built` guard |
| M5 | Context injection assumes the param is literally named `ctx` — `context: Context` gets a TypeError | `tool_wrapper.py:84-95` | use detected param's name |
| M6 | Module import failures drop all of that module's tools with one WARNING; inspect-pass failures logged at DEBUG; no end-of-build summary | `surface.py:382-384`, `app.py:112-114`, `_registry.py:144-145` | ERROR + "N modules failed, M tools skipped" summary; surface in `/health`/`validate` |
| M7 | Extraction cache keyed on single-file content only — editing a base class/decorator in another module serves stale inspect data | `cache.py:64-73` | fold dependency hashes into key, or cache AST-only portion |
| M8 | `extraction_result` public property is permanently `None` | `app.py:296,756-759` | assign in `discover()` (natural carrier for C5's errors) |
| M9 | Per-call hot-path waste: coercion re-parses type strings and re-runs `inspect.signature` per call; schema endpoint recomputes per request; eviction scan runs O(buckets) under a global lock twice per request (stacked middlewares) | `coercion.py:38-64`, `schema_endpoint.py:35-81`, `security.py:166-183` | compile a per-tool coercion plan at wrap time; memoize schemas; amortize eviction with a `_last_evict` timestamp |
| M10 | Startup: all modules imported (with side effects) before filters are applied; `infer_return_type` re-walks the whole module AST per unannotated callable (O(callables × nodes)) | `app.py:339-344`, `type_inference.py:90-132` | filter before inspect pass; index function nodes in one walk |
| M11 | No `py.typed` — the well-annotated public API is invisible to mypy/pyright consumers | packaging | ship empty `py.typed` in the wheel |
| M12 | CLI: no `--version`; `serve -h` is `--host` not help (errors confusingly); `test --params` without `--tool` silently ignored | `cli/main.py:60`, `app.py:638-643` | `@click.version_option`; `-H` for host; usage error |
| M13 | Version hardcoded twice (`pyproject.toml:7`, `__init__.py:6`); no single-sourcing; release branch naming already drifted | packaging | hatch dynamic version + tag-driven releases |
| M14 | `@toolkit(lifecycle="sesion")` silently accepted — decorator takes any string while the manifest uses `Literal` | `_decorators.py:106-112` | validate against the same Literal; add `ParamSpec` so decorators preserve signatures for IDEs |
| M15 | README defects: "✅ Schema validation" bullet ×3, type-coercion bullet missing its prefix, banner hotlinked from `github user-attachments` (breaks on PyPI); architecture.md CLI section scrambled; docs reference stale project name `pymcp` | `README.md:87-90,1`, `docs/architecture.md:104-112`, `docs/README.md:827` | dedupe, vendor asset under `docs/assets/`, rebuild CLI block |
| M16 | `discover_module(pd.DataFrame)` README example passes a class where the API takes a module (root cause of half of C4) | `README.md:27` | fix example or genuinely support classes |
| M17 | Prompt injection: third-party docstrings flow unescaped into LLM description prompts; generated descriptions become authoritative tool guidance for downstream agents | `llm/generator.py:104-134` | delimit/escape untrusted docstring text; flag descriptions from untrusted sources |
| M18 | Per-session rate limit bucket keyed on client-controlled `session_id` — reconnect resets the window; only the (off-by-default) global limiter backstops; no per-IP limiting | `security.py:102-112,202-206` | socket-derived per-IP limiter; document per-session limits as advisory |
| M19 | `dict`/`list` coercion passes arbitrary strings to `json.loads` with no size/depth bound | `coercion.py:161-169` | length cap + depth guard |
| M20 | Dev tooling floor: ruff selects only `E,F,I,W` (no B/S/UP/RUF), no mypy, no pre-commit, dev deps not in a dependency-group (plain `uv sync` lacks pytest), no CONTRIBUTING | `pyproject.toml` | expand ruleset, add mypy/pre-commit, `[dependency-groups]`, fix the 112 errors |

## Low (compressed)

- Unused imports (`textwrap`, `importlib.util` in `surface.py:20,25`); `models.py:115-117` `non_self_params` drops params *named* `self` on free functions; `_registry.py:126` toolkits keyed by bare class name (cross-module collision); `app.py:262` `if port:` treats `port=0`/falsy name as unset.
- Docstring parser: Sphinx inline-type form (`:param str name:`) unparsed; `*args` entries bleed into the previous param's description (`docstrings.py:303,123`).
- `type_inference.py:138` counts nested functions' returns as the outer function's; `__all__` detection matches assignments inside functions and ignores `AnnAssign`/`+=` (`surface.py:332-339`) — and a dynamic `__all__` silently disables `respect_all` entirely.
- `init` with a nonexistent PATH inverts its own contract (manifest written to PATH, `--output` becomes the scan path) — `cli/main.py:292-297`.
- Schema inconsistency: signature path emits `"default": null`, extracted path omits it; defaults not checked for JSON-serializability before `JSONResponse`.
- `find_manifest` selection is silent — log which manifest was adopted at INFO.
- No file-size guard before `read_text()`/`ast.parse` (a 50MB or deeply nested file → slow parse or RecursionError landing in the silently-discarded warnings list).
- `.gitignore` carries personal entries (`pending.md`, `/docs/superpowers`, `.claude/settings.json`) in a public package repo; `smarter-mcp-spec.md` (713-line design doc) belongs under `docs/`.
- `b64decode(validate=False)` lets garbage strings proceed to a doomed PIL open; ~3 concurrent payload copies in the image pipeline.
- `cors_origins` defaults to `["*"]` — currently inert, unsafe the day it's wired.
- LLM description cache grows monotonically (stale entries never pruned).

---

## What's genuinely good

- **`SlidingWindowMiddleware`** (`security.py:136-191`) is the best piece of engineering in the tree: correct deque windowing under an `anyio.Lock`, inline stale-bucket eviction that fixes a real unbounded-memory flaw in FastMCP's built-in limiter, with a docstring explaining exactly why it exists. It also correctly refuses to trust spoofable IP headers.
- **The manifest layer** (`config/manifest.py`): Pydantic v2 with `Literal` constraints, `${VAR:default}` env substitution that fails loudly on missing vars, YAML null-list normalization. `yaml.safe_load` throughout; no `eval`/`exec` anywhere (defaults via `ast.literal_eval`).
- **`sys.path` mutation discipline**: snapshot/restore under a shared lock at both import sites, with comments explaining the parallelization concern.
- **Scan hygiene** (`surface.py:690-727`): `pyvenv.cfg` venv detection, comprehensive dir pruning via in-place `dirnames[:]`, loud >500-file warning — prevents the classic "served my whole home directory" pathology.
- **The extraction cache design** (modulo the pickle and keying issues): content-hash keys, in-memory layer, atomic tmp-file replace, version stamping, every failure degrades to a miss.
- **The docstring parser** handles Google/NumPy/Sphinx with auto-detection and has real test coverage; AST parameter handling (pos-only, kw-only, variadics, default alignment) is correct.
- **Docs honesty in places**: the "Implemented vs Coming Soon" section is accurate; `export` is correctly documented as a stub; per-tool LLM failure isolation is the right instinct.
- **No committed secrets** anywhere in source or git history; API keys read from env only, never logged.

---

## Remediation roadmap (ordered)

**Phase 0 — stop the bleeding (hours):**
1. C1: gate image coercion on return type. 2. H9: fix `_union_members` one-liner. 3. H17/H18: un-ignore `/tests`, delete the root manifest. 4. H4: stop returning tracebacks; raise `ToolError`.

**Phase 1 — make the advertised product true (days):**
5. C2: Context injection + session cleanup hooks. 6. C3: constructor arg order + bounded `find_manifest` + nonexistent-path errors. 7. C4: package/class `discover_module`. 8. C5: surface extraction errors in `discover`/`validate`/`health`. 9. H10–H13: static/classmethods, schema type mapping, naming/collisions, property resources.

**Phase 2 — production hardening (days):**
10. H1–H3, H7, M18: secure defaults, SSRF/image limits, constant-time compare, per-IP limiting. 11. H5: de-pickle the cache. 12. H6 + H17: CI matrix, trusted publishing, SHA-pinned actions. 13. H14–H16, H19, H20: manifest path resolution, `extra="forbid"` + dead-config reconciliation, LLM timeouts/batching, `[llm]` extra, instance locks.

**Phase 3 — polish:** Mediums (coercion strictness, py.typed, CLI UX, version single-sourcing, perf precompilation, README/docs corrections), then Lows.

**Testing mandate cutting across all phases:** the next test written should start a real server and call a real tool over MCP. Priority order for new coverage: `runtime/` (wrapper + coercion + instances) → `server/security.py` → `config/manifest.py` → `cli/`. None of the five Critical findings could exist if one end-to-end "serve and call `greet`" test existed.

## Residual risk statement

This review executed the main runtime paths but did not fuzz the extractor, load-test the server, or audit FastMCP itself (one dependency layer down — e.g. `StaticTokenVerifier`'s own timing behavior is out of scope). The performance findings are code-derived estimates, not profiled measurements. Findings marked "reproduced"/"verified" were executed against FastMCP 3.3.1 on CPython 3.12; behavior on other FastMCP majors was not tested.

**Senior-review survivability:** extractor, manifest, and rate-limiter layers — yes. Runtime, multimodal, lifecycle, CI/CD, and docs-accuracy — no, not in current state. The honest framing for the README today is the one already in pyproject: Alpha.

---

## Friday, 12-06-2026, 11:42 am, [main] Principal addendum - Codex RTK pass

### Validation / correction

Fable 5's verdict stands. I re-read the runtime, router, manifest, schema, testing, auth, packaging, docs, RTK contract, and repo memory. The major blockers are correctly placed: runtime wrappers, lifecycle, discovery, silent extraction failure, insecure defaults, no CI, and docs drift.

One sharpening correction: `smarter-mcp test` is not a partial safety net for C1/C2. It is a false-confidence path. The runner explicitly calls raw `tool.fn` (`_testing.py:128-134`, `_testing.py:340-350`) while production calls the wrapper (`tool_wrapper.py:98,115,163,181`) and then `coerce_to_fastmcp_image()` (`interceptor.py:112-123`). Repro: a `str`-returning `greet` test passes (`Results: 1 passed`) while the wrapper converts the same `str` result into `FastMCP.Image`. Treat current tool tests as unit smoke tests only, not MCP production validation.

### Additional findings

**A1. HIGH - `/schema` leaks and lies about tools hidden by manifest policy.** Router suppression happens only during FastMCP registration (`router.py:183-189`), but `SchemaEndpoint` reads the raw registry (`schema_endpoint.py:35-48`, `schema_endpoint.py:61-66`). Repro: a tool with `ToolOverride(expose=False)` still appears in compact schema as `{'name': 'dangerous', 'params': ['token']}`. This also ignores manifest renames/descriptions, class-prefix routing names, and registration failures. For real products this leaks intentionally hidden internal capability shape to any caller allowed to hit `/mcp/{namespace}/schema` (and unauthenticated callers when auth is off). Fix: build schema from router-registered tools, apply the same override decision, and return 404/403 for suppressed namespaces/tools.

**A2. HIGH - `build()` returns a partially secured server object; auth behavior depends on which public entrypoint users choose.** `build_auth_provider()` is attached inside `build()` (`app.py:573-574`), but the custom header middleware is attached only by `http_app()` and `run()` (`app.py:674-710`, `app.py:727-741`). If `auth_enabled=True` but no keys are present, `build_auth_provider()` returns `None` (`security.py:89-94`); a user who does `server = app.build(); server.run(...)` bypasses `_asgi_middleware()` and can get a fail-open server. Fix: fail startup when auth is enabled and no keys are loaded, and do not expose a public built server path that omits the same auth middleware used by `SmarterMCP.run()`.

**A3. MEDIUM - Release compatibility is unbounded while the code imports FastMCP internals.** `fastmcp>=3.0` has no upper bound (`pyproject.toml:28`), yet the code imports deep module paths such as `fastmcp.server.middleware.middleware` and `fastmcp.server.auth.providers.jwt.StaticTokenVerifier` (`security.py:27`, `security.py:96`). This is not a stable integration posture for a package claiming Python 3.10-3.13 support. Fix: pin a tested FastMCP range, add a compatibility matrix in CI, and wrap internal imports behind a local adapter.

**A4. MEDIUM - MIT is declared but no license file ships in the repository.** Metadata says `license = "MIT"` and classifier says MIT (`pyproject.toml:10`, `pyproject.toml:19`), but no `LICENSE`/`COPYING` file exists in the repo file list. That is a legal/distribution hygiene miss, not a runtime bug. Fix: add the MIT license text, include it in source/wheel metadata, and keep copyright ownership explicit.

### Prioritization update

Add A1 and A2 to Phase 0 after C1/H4. The first production hardening loop should be: wrapper return handling, sanitized errors, real MCP test harness, schema built from the routed server surface, auth fail-closed on every public entrypoint, then CI/publish gates. Without those, every later remediation can still ship with green local tests and a misleading introspection endpoint.
