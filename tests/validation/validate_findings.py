"""End-to-end validation of the production-assessment findings.

Each check builds a *real* SmarterMCP server and, where relevant, calls tools
through FastMCP's in-memory client transport — the same path a real MCP client
uses. A check "CONFIRMED" means the reported bug reproduced; "NOT REPRODUCED"
means the code behaved correctly and the finding is wrong or already fixed.

Run with stdout+stderr redirected to logs/ (see run_validation.sh).
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import tempfile
import traceback
from pathlib import Path

results: list[tuple[str, bool, str]] = []


def record(finding: str, confirmed: bool, detail: str) -> None:
    status = "CONFIRMED (bug reproduced)" if confirmed else "NOT REPRODUCED"
    print(f"\n{'='*78}\n[{finding}] {status}\n  {detail}\n{'='*78}", flush=True)
    results.append((finding, confirmed, detail))


def section(title: str) -> None:
    print(f"\n\n########## {title} ##########", flush=True)


# ---------------------------------------------------------------------------
# C1 — str/bytes/Path tool returns are coerced into fastmcp.Image and fail
# ---------------------------------------------------------------------------
async def check_c1_string_return() -> None:
    section("C1: string-returning tool")
    from fastmcp import Client
    from smarter_mcp import SmarterMCP, tool

    app = SmarterMCP(name="c1-server")

    @tool("Greet a user by name")
    def greet(name: str) -> str:
        return f"Hello, {name}!"

    server = app.build()
    try:
        async with Client(server) as client:
            # H12 fix: "default" namespace mounted without prefix → tool is "greet"
            res = await client.call_tool("greet", {"name": "Ada"})
            text = res.data if hasattr(res, "data") else res
            ok_value = text == "Hello, Ada!" or (
                getattr(res, "content", None)
                and getattr(res.content[0], "text", None) == "Hello, Ada!"
            )
            record(
                "C1",
                not ok_value,
                f"greet returned {text!r} (content={getattr(res,'content',None)!r}); "
                f"expected the plain string back",
            )
    except Exception as e:  # noqa: BLE001 - we are probing for the failure
        record(
            "C1",
            True,
            f"calling greet raised {type(e).__name__}: {e!r} "
            f"(string return coerced into an Image path)",
        )


# ---------------------------------------------------------------------------
# C2 — @toolkit(lifecycle="session") silently degrades to per-call
# ---------------------------------------------------------------------------
async def check_c2_session_lifecycle() -> None:
    section("C2: session lifecycle")
    import importlib

    from fastmcp import Client
    from smarter_mcp import SmarterMCP

    # Write the toolkit to a real importable module so class resolution works
    # (this is the realistic discovery path; a function-local class can't be
    # resolved by getattr(module, class_name)).
    tmp = Path(tempfile.mkdtemp())
    sys.path.insert(0, str(tmp))
    mod = tmp / "c2_toolkit.py"
    mod.write_text(
        "from smarter_mcp import tool, toolkit\n\n"
        "@toolkit(lifecycle='session')\n"
        "class Counter:\n"
        "    def __init__(self):\n"
        "        self.n = 0\n"
        "    @tool(name='bump')\n"
        "    def bump(self) -> int:\n"
        "        self.n += 1\n"
        "        return self.n\n"
    )
    importlib.import_module("c2_toolkit")

    app = SmarterMCP(name="c2-server")
    server = app.build()
    try:
        async with Client(server) as client:
            # Find the registered tool name (prefixing is itself a finding).
            names = [t.name for t in await client.list_tools()]
            bump_name = next((n for n in names if n.endswith("bump")), None)
            if bump_name is None:
                record("C2", True, f"no bump tool registered at all; tools={names}")
                return
            r1 = await client.call_tool(bump_name, {})
            r2 = await client.call_tool(bump_name, {})

            def val(r):
                if hasattr(r, "data") and r.data is not None:
                    return r.data
                if getattr(r, "content", None):
                    return getattr(r.content[0], "text", None)
                return r

            v1, v2 = val(r1), val(r2)
            # Session state working => second call returns 2.
            degraded = str(v2) in ("1", "1.0")
            record(
                "C2",
                degraded,
                f"two bump() calls in one session returned {v1!r} then {v2!r}; "
                f"session state working would give 1 then 2 (tool name: {bump_name!r})",
            )
    except Exception as e:  # noqa: BLE001
        record("C2", True, f"session toolkit call raised {type(e).__name__}: {e!r}")


# ---------------------------------------------------------------------------
# C3 — SmarterMCP("my-server") treats the name as a source path
# ---------------------------------------------------------------------------
def check_c3_positional_name() -> None:
    section("C3: positional name treated as source_root")
    from smarter_mcp import SmarterMCP

    # Run from a temp dir so a stray parent manifest can't interfere.
    import os

    prev = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        os.chdir(td)
        try:
            app = SmarterMCP("my-server")
            cfg = app.config
            name = getattr(cfg, "name", None)
            sources = getattr(cfg, "sources", None)
            confirmed = name != "my-server"
            record(
                "C3",
                confirmed,
                f'SmarterMCP("my-server") -> config.name={name!r}, sources={sources!r}; '
                f"the string was parsed as a path, not the server name",
            )
        finally:
            os.chdir(prev)


# ---------------------------------------------------------------------------
# C4 — discover_module on a package / a class
# ---------------------------------------------------------------------------
def check_c4_discover_module() -> None:
    section("C4: discover_module on package and class")
    from smarter_mcp import SmarterMCP

    # 4a: a package (json is a package-like stdlib module with __init__).
    import json

    app = SmarterMCP(name="c4-pkg")
    try:
        app.discover_module(json, include=["dumps", "loads"])
        tlist = app._registry.get_all_tools()
        names = [t.name for t in tlist]
        n = len(tlist)
        record(
            "C4a",
            n == 0,
            f"discover_module(json, include=['dumps','loads']) registered {n} tools "
            f"(names={names})",
        )
    except Exception as e:  # noqa: BLE001
        record("C4a", True, f"discover_module(json,...) raised {type(e).__name__}: {e!r}")

    # 4b: a class (README's pd.DataFrame example shape) — use a stdlib class.
    from collections import Counter as CCounter

    app2 = SmarterMCP(name="c4-cls")
    try:
        app2.discover_module(CCounter, include=["most_common"])
        tlist = app2._registry.get_all_tools()
        # The bug: params empty and class_name None (unbound, schema-less)
        bad = []
        for t in tlist:
            eo = getattr(t, "extracted_obj", None)
            params = getattr(eo, "parameters", None) if eo else None
            cls = getattr(t, "class_name", None)
            bad.append((getattr(t, "name", "?"), cls, len(params) if params else 0))
        confirmed = any(cls is None for (_n, cls, _p) in bad) if bad else False
        record(
            "C4b",
            confirmed,
            f"discover_module(collections.Counter, include=['most_common']) -> "
            f"tools (name, class_name, n_params) = {bad}; class_name=None means "
            f"unbound methods registered via the inspect-only fallback",
        )
    except Exception as e:  # noqa: BLE001
        record("C4b", True, f"discover_module(Counter,...) raised {type(e).__name__}: {e!r}")


# ---------------------------------------------------------------------------
# C5 — extraction errors silently discarded; validate lies
# ---------------------------------------------------------------------------
def check_c5_silent_extraction_errors() -> None:
    section("C5: silent extraction errors")
    from smarter_mcp import SmarterMCP

    with tempfile.TemporaryDirectory() as td:
        broken = Path(td) / "broken.py"
        broken.write_text("def oops(:\n    return 1\n")  # syntax error
        good = Path(td) / "ok.py"
        good.write_text("def fine(x: int) -> int:\n    return x\n")

        app = SmarterMCP(source_root=td, use_inspect=False)
        try:
            app.discover(td)
            tools = app._registry.get_all_tools()
            names = sorted(t.name for t in tools)
            extraction = getattr(app, "extraction_result", None)
            # The bug: the broken file's tool silently vanishes (only `fine` remains),
            # discover() raised nothing, and the public error carrier is never populated.
            confirmed = ("default_fine" in names or "fine" in names) and extraction is None
            record(
                "C5",
                confirmed,
                f"discover() over a dir containing a syntax-error file: registered tools="
                f"{names}, app.extraction_result={extraction!r}. The broken file produced no "
                f"error to the caller; only the valid file's tool survived.",
            )
        except Exception as e:  # noqa: BLE001
            record("C5", False, f"discover() actually raised (good): {type(e).__name__}: {e!r}")


# ---------------------------------------------------------------------------
# H9 — RecursionError coercing list[int | None]
# ---------------------------------------------------------------------------
def check_h9_recursion() -> None:
    section("H9: nested-union coercion recursion")
    from smarter_mcp.runtime import coercion

    try:
        out = coercion._coerce_value_from_str("[1, 2]", "list[int | None]", "p")
        record("H9", False, f"coerced cleanly to {out!r} (no recursion)")
    except RecursionError:
        record("H9", True, "coercing list[int | None] raised RecursionError")
    except Exception as e:  # noqa: BLE001
        record("H9", False, f"raised {type(e).__name__}: {e!r} (not RecursionError)")


# ---------------------------------------------------------------------------
# H11 — JSON schema maps Optional/Union/Literal to "string"
# ---------------------------------------------------------------------------
def check_h11_schema_types() -> None:
    section("H11: schema type mapping")
    from smarter_mcp import SmarterMCP
    from smarter_mcp._schema import build_json_schema

    # Build a real tool from a source file with rich annotations, then inspect
    # the JSON schema actually published for it.
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "typed_tools.py"
        src.write_text(
            "from typing import Optional, List, Literal\n\n"
            "def f(a: Optional[int], b: List[int], c: Literal['x', 'y'], d: int) -> None:\n"
            "    pass\n"
        )
        app = SmarterMCP(source_root=td, use_inspect=False)
        app.discover(td)
        tools = app._registry.get_all_tools()
        target = next((t for t in tools if t.name.endswith("f")), None)
        if target is None:
            record("H11", False, f"could not find tool 'f' (tools={[t.name for t in tools]})")
            return
        schema = build_json_schema(target)
        props = schema.get("properties", {})
        types = {k: v.get("type") for k, v in props.items()}
        # a (Optional[int]) and b (List[int]) and c (Literal) all collapse to "string"
        # while d (plain int) maps to "integer". That mismatch is the finding.
        wrong = {
            k: t
            for k, t in types.items()
            if k in ("a", "b", "c") and t == "string"
        }
        record(
            "H11",
            len(wrong) > 0,
            f"published schema types: {types}; Optional/List/Literal collapsed to 'string': "
            f"{wrong} (plain int 'd' -> {types.get('d')!r})",
        )


# ---------------------------------------------------------------------------
# H15 — manifest silently ignores unknown keys (no extra='forbid')
# ---------------------------------------------------------------------------
def check_h15_unknown_keys() -> None:
    section("H15: manifest ignores unknown keys")
    from smarter_mcp.config import manifest as mani

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "smarter-mcp.yaml"
        p.write_text(
            "name: t\n"
            "expose:\n"
            "  private: false\n"            # README key; real key is include_private
            "  unannotated: warn\n"          # README key; real key is unannotated_policy
            "  totally_made_up_key: 123\n"
        )
        try:
            cfg = mani.load_manifest(str(p))
            # If it loaded without error, the bogus keys were silently ignored.
            record(
                "H15",
                True,
                f"manifest with bogus keys (private/unannotated/totally_made_up_key) "
                f"loaded clean: name={cfg.name!r} — no extra='forbid', typos are silent",
            )
        except Exception as e:  # noqa: BLE001
            record("H15", False, f"load_manifest rejected unknown keys (good): {e!r}")


# ---------------------------------------------------------------------------
# H1 — insecure defaults (0.0.0.0 + auth off)
# ---------------------------------------------------------------------------
def check_h1_insecure_defaults() -> None:
    section("H1: insecure defaults")
    from smarter_mcp.config.manifest import ServerConfig

    sc = ServerConfig()
    host = getattr(sc, "host", None)
    auth = getattr(sc, "auth_enabled", None)
    rl = getattr(sc, "rate_limit_enabled", None)
    confirmed = host == "0.0.0.0" and not auth
    record(
        "H1",
        confirmed,
        f"ServerConfig defaults: host={host!r}, auth_enabled={auth!r}, "
        f"rate_limit_enabled={rl!r}",
    )


def main() -> int:
    print("smarter-mcp finding validation harness", flush=True)
    print(f"python={sys.version}", flush=True)
    try:
        import fastmcp

        print(f"fastmcp={fastmcp.__version__}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"fastmcp import failed: {e!r}", flush=True)

    # Sync checks
    for fn in (
        check_c3_positional_name,
        check_c4_discover_module,
        check_c5_silent_extraction_errors,
        check_h9_recursion,
        check_h11_schema_types,
        check_h15_unknown_keys,
        check_h1_insecure_defaults,
    ):
        try:
            fn()
        except Exception:  # noqa: BLE001
            print(f"\n[HARNESS ERROR in {fn.__name__}]", flush=True)
            traceback.print_exc()

    # Async checks (real in-memory MCP client calls)
    for coro in (check_c1_string_return, check_c2_session_lifecycle):
        try:
            asyncio.run(coro())
        except Exception:  # noqa: BLE001
            print(f"\n[HARNESS ERROR in {coro.__name__}]", flush=True)
            traceback.print_exc()

    section("SUMMARY")
    confirmed = [f for f, c, _ in results if c]
    notrepro = [f for f, c, _ in results if not c]
    for finding, c, detail in results:
        print(f"  {'[CONFIRMED]    ' if c else '[NOT REPRODUCED]'} {finding}: {detail[:90]}", flush=True)
    print(f"\nCONFIRMED: {len(confirmed)} -> {confirmed}", flush=True)
    print(f"NOT REPRODUCED: {len(notrepro)} -> {notrepro}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
