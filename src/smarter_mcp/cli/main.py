"""Main CLI entry points — click subcommands for serving, validating, testing, and scaffolding."""

from __future__ import annotations

import inspect
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import click

from smarter_mcp.cli._detect import resolve_target
from smarter_mcp.server.app import SmarterMCP


BANNER = r"""
┌─┐ ┌┬┐ ┌─┐ ┬─┐ ┌┬┐ ┌─┐ ┬─┐
└─┐ │││ ├─┤ ├┬┘  │  ├┤  ├┬┘ - MCP
└─┘ ┴ ┴ ┴ ┴ ┴└─  ┴  └─╸ ┴└─
"""


def print_banner(err: bool = False) -> None:
    """Print the Smarter-MCP ASCII art banner."""
    click.echo(click.style(BANNER.strip("\n"), fg="cyan", bold=True), err=err)


class BannersGroup(click.Group):
    """Custom Click Group to show the ASCII art banner on help screens."""
    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        print_banner(err=False)
        super().format_help(ctx, formatter)


@click.group(cls=BannersGroup)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging (DEBUG level).")
@click.option("--quiet", "-q", is_flag=True, help="Only log errors (ERROR level).")
def cli(verbose: bool, quiet: bool):
    """Smarter-MCP CLI — serve, validate, test, scaffold, and export servers."""
    log_level = logging.INFO
    if quiet:
        log_level = logging.ERROR
    elif verbose:
        log_level = logging.DEBUG

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        force=True
    )


@cli.command()
@click.argument("target", required=False)
@click.option("--manifest", "-m", help="Path to manifest YAML file.")
@click.option("--port", "-p", type=int, help="Port to run the server on (SSE/HTTP only).")
@click.option("--host", "-h", help="Host address to bind to (SSE/HTTP only).")
@click.option("--transport", "-t", type=click.Choice(["sse", "streamable-http", "stdio"]), help="Transport type.")
@click.option("--dev", is_flag=True, help="Enable hot-reloading dev mode.")
def serve(
    target: str | None,
    manifest: str | None,
    port: int | None,
    host: str | None,
    transport: str | None,
    dev: bool
):
    """Start the MCP server."""
    if dev:
        try:
            import watchfiles
        except ImportError:
            raise click.ClickException(
                "Development dependency 'watchfiles' is required for hot-reloading (--dev). "
                "Install it with `pip install smarter-mcp[dev]` or `pip install watchfiles`."
            )

        # Resolve directory/parent directory to watch
        watch_path = Path.cwd()
        if target:
            tp = Path(target).resolve()
            if tp.is_file():
                watch_path = tp.parent
            elif tp.is_dir():
                watch_path = tp
        elif manifest:
            watch_path = Path(manifest).resolve().parent

        click.echo(click.style(f"Starting server in DEV mode (watching: {watch_path})...", fg="cyan", bold=True))

        def run_target():
            # Hot-reload respawns this subprocess on every change; enable the
            # disk extraction cache so unchanged files aren't re-extracted.
            os.environ.setdefault("SMARTER_MCP_EXTRACTION_CACHE", "1")
            # Ensure the watched directory is in Python path for imports
            if str(watch_path) not in sys.path:
                sys.path.insert(0, str(watch_path))
            try:
                app = resolve_target(target, manifest)
                if port is not None:
                    app.config.server.port = port
                if host is not None:
                    app.config.server.host = host
                if transport is not None:
                    app.config.server.transport = transport
                
                # Print banner
                active_transport = app.config.server.transport
                if active_transport == "stdio":
                    print_banner(err=True)
                else:
                    print_banner(err=False)

                app.run()
            except Exception as e:
                click.echo(click.style(f"Error in dev execution: {e}", fg="red"), err=True)

        watchfiles.run_process(watch_path, target=run_target)
    else:
        app = resolve_target(target, manifest)
        if port is not None:
            app.config.server.port = port
        if host is not None:
            app.config.server.host = host
        if transport is not None:
            app.config.server.transport = transport
        
        # Print banner
        active_transport = app.config.server.transport
        if active_transport == "stdio":
            print_banner(err=True)
        else:
            print_banner(err=False)

        app.run()


@cli.command()
@click.argument("target", required=False)
@click.option("--manifest", "-m", help="Path to manifest YAML file.")
def validate(target: str | None, manifest: str | None):
    """Dry-run validation of the MCP server."""
    click.echo(click.style("=== Smarter-MCP Server Validation ===", fg="cyan", bold=True))

    app = resolve_target(target, manifest)
    app.build()

    # C5: surface extraction errors and warnings so the user knows which files
    # failed to parse, and exit non-zero when there are errors.
    extraction = getattr(app, "extraction_result", None)
    has_errors = False
    if extraction is not None:
        if extraction.errors:
            has_errors = True
            click.echo(click.style(
                f"Extraction Errors ({len(extraction.errors)}):", fg="red", bold=True
            ))
            for err in extraction.errors:
                click.echo(click.style(f"  ERROR: {err}", fg="red"))
        if extraction.warnings:
            click.echo(click.style(
                f"Extraction Warnings ({len(extraction.warnings)}):", fg="yellow", bold=True
            ))
            for w in extraction.warnings:
                click.echo(click.style(f"  WARNING: {w}", fg="yellow"))

    click.echo(f"Server Name: {app.config.name}")
    click.echo(f"Transport:   {app.config.server.transport}")
    click.echo(f"Host/Port:   {app.config.server.host}:{app.config.server.port}")
    click.echo("-" * 40)

    # C5: exit non-zero here so the early-return path below doesn't swallow errors.
    if has_errors:
        click.echo(click.style(
            "✗ Validation failed: extraction errors found (see above).",
            fg="red", bold=True,
        ))
        sys.exit(1)

    namespaces = app._registry.get_all_namespaces()
    if not namespaces:
        click.echo(click.style("No tools or resources registered.", fg="yellow"))
        return

    warnings: list[str] = []

    for ns in sorted(namespaces):
        tools = app._registry.get_namespace_tools(ns)
        resources = app._registry.get_namespace_resources(ns)

        click.echo(click.style(f"Namespace: {ns}", bold=True, fg="green"))
        click.echo(f"  Tools:     {len(tools)}")
        click.echo(f"  Resources: {len(resources)}")

        for tool in tools:
            sig = inspect.signature(tool.fn)
            params = [p for p in sig.parameters.values() if p.name != "self"]
            param_count = len(params)
            ret_type = sig.return_annotation
            ret_str = ret_type.__name__ if hasattr(ret_type, "__name__") else str(ret_type)
            if ret_str == "inspect._empty":
                ret_str = "Any"

            desc = tool.description or ""
            desc_trunc = desc[:50] + "..." if len(desc) > 50 else desc

            click.echo(f"    - Tool: {click.style(tool.name, bold=True)}")
            click.echo(f"      Description: {desc_trunc}")
            click.echo(f"      Parameters:  {param_count}")
            click.echo(f"      Return Type: {ret_str}")

            # Check for warnings
            unannotated_params = []
            for p in params:
                if p.annotation == inspect.Parameter.empty:
                    unannotated_params.append(p.name)
            if unannotated_params:
                warnings.append(
                    f"Tool '{tool.name}' (namespace '{ns}') has parameters without type annotations: {', '.join(unannotated_params)}"
                )

            has_var = any(p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD) for p in params)
            if has_var:
                warnings.append(
                    f"Tool '{tool.name}' (namespace '{ns}') has variadic parameters (*args or **kwargs) which might not be supported properly by clients."
                )

        for res in resources:
            click.echo(f"    - Resource: {click.style(res.uri, bold=True, fg='blue')}")
            desc = res.description or ""
            desc_trunc = desc[:50] + "..." if len(desc) > 50 else desc
            click.echo(f"      Description: {desc_trunc}")

    if warnings:
        click.echo("-" * 40)
        click.echo(click.style(f"Warnings ({len(warnings)}):", fg="yellow", bold=True))
        for w in warnings:
            click.echo(click.style(f"  ○ {w}", fg="yellow"))

    if not warnings:
        click.echo("-" * 40)
        click.echo(click.style("✓ Validation successful! No issues or warnings found.", fg="green", bold=True))


@cli.command()
@click.argument("target", required=False)
@click.option("--manifest", "-m", help="Path to manifest YAML file.")
@click.option("--tool", "tool_name", help="Name of a specific tool to test.")
@click.option("--params", "params_json", help="Ad-hoc parameter values (JSON string) for testing.")
def test(
    target: str | None,
    manifest: str | None,
    tool_name: str | None,
    params_json: str | None
):
    """Run predefined or ad-hoc tool tests."""
    click.echo(click.style("=== Running Tool Tests ===", fg="cyan", bold=True))
    if tool_name:
        click.echo(f"Targeting tool: {tool_name}")
    if params_json:
        click.echo(f"Ad-hoc params:  {params_json}")
    click.echo("-" * 40)

    app = resolve_target(target, manifest)
    app.build()

    params: dict[str, Any] | None = None
    if params_json:
        try:
            params = json.loads(params_json)
        except json.JSONDecodeError as e:
            raise click.ClickException(f"Invalid JSON in --params: {e}")

    try:
        report = app.test(tool_name=tool_name, params=params, verbose=False)
    except ValueError as e:
        raise click.ClickException(str(e))

    for result in report.results:
        status_symbol = click.style("✓ PASS", fg="green", bold=True) if result.passed else click.style("✗ FAIL", fg="red", bold=True)
        click.echo(f"{status_symbol}  {click.style(result.namespace, fg='white', dim=True)}/{click.style(result.tool_name, bold=True)} ({result.latency_ms:.1f}ms)")
        if not result.passed and result.error:
            click.echo(click.style(f"  Error: {result.error}", fg="red"))

    click.echo("-" * 40)
    summary_style = "green" if report.failed == 0 else "red"
    click.echo(click.style(f"Test Summary: {report.passed} passed, {report.failed} failed, {report.skipped} skipped", fg=summary_style, bold=True))

    if report.failed > 0:
        sys.exit(1)


@cli.command()
@click.argument("path", default=".", required=False)
@click.option("--output", "-o", "output", default=None,
              help="Directory to write smarter-mcp.yaml into (default: current working directory).")
@click.option("--force", "-f", is_flag=True, help="Overwrite existing smarter-mcp.yaml if it exists.")
def init(path: str, output: str | None, force: bool):
    """Scaffold a smarter-mcp.yaml manifest in the current working directory.

    PATH is the file or directory to scan for tools (default: current directory).
    The manifest is always written to the current working directory unless
    --output is given.

    Examples:

      smarter-mcp init                              # scan . → write ./smarter-mcp.yaml
      smarter-mcp init ./app/services/my_tools.py  # scan file → write ./smarter-mcp.yaml
      smarter-mcp init ./src/mylib                 # scan dir  → write ./smarter-mcp.yaml
      smarter-mcp init ./src -o ./config           # scan ./src → write ./config/smarter-mcp.yaml
    """
    given_path = Path(path).resolve()

    # If the given path doesn't exist, treat it as the output directory (create
    # it) and fall back to scanning the cwd. This preserves the original
    # `smarter-mcp init ./new-project` workflow where the dir is scaffolded fresh.
    if not given_path.exists():
        output_dir = given_path
        scan_path = Path(output).resolve() if output else Path.cwd()
    else:
        scan_path = given_path
        output_dir = Path(output).resolve() if output else Path.cwd()

    if not scan_path.exists():
        raise click.ClickException(f"Source path does not exist: {scan_path}")

    server_name = output_dir.name or "my-mcp-server"

    output_file = output_dir / "smarter-mcp.yaml"
    if output_file.exists() and not force:
        raise click.ClickException(
            f"File '{output_file}' already exists. Use --force to overwrite."
        )

    # Determine source_root and the path written into the manifest's sources block.
    # We make it relative to output_dir so the YAML is portable — if the user
    # runs the server from the same directory as the manifest it will just work.
    if scan_path.is_file():
        source_root = scan_path.parent
        try:
            source_path_for_manifest = str(scan_path.relative_to(output_dir))
        except ValueError:
            source_path_for_manifest = str(scan_path)  # absolute fallback if not under output_dir
    else:
        source_root = scan_path
        try:
            source_path_for_manifest = str(scan_path.relative_to(output_dir))
        except ValueError:
            source_path_for_manifest = str(scan_path)  # absolute fallback

    # Scrape files to pre-populate comments
    try:
        app = SmarterMCP(source_root=source_root)
        if scan_path.is_file():
            from smarter_mcp.extractor.surface import SurfaceExtractor
            extractor = SurfaceExtractor(source_root=source_root)
            from smarter_mcp.extractor.filters import apply_filters
            from smarter_mcp.server.app import _resolve_implementations, _exposure_rules_from_config
            extraction = extractor.extract_file(scan_path)
            from smarter_mcp.extractor.models import ExtractionResult
            result = ExtractionResult(modules=[extraction])
            impls, _failed, _skipped = _resolve_implementations(result, str(source_root))
            rules = _exposure_rules_from_config(app._config)
            filtered = apply_filters(result, rules)
            app._registry.merge_extraction(filtered, impls)
        else:
            app.build()
        discovered_tools = app._registry.get_all_tools()
    except Exception as e:
        click.echo(click.style(f"Warning during directory scan: {e}", fg="yellow"))
        discovered_tools = []

    tools_section = []
    if discovered_tools:
        tools_section.append("  # Discovered tools ready for overrides/tests:")
        for tool in discovered_tools:
            qual_name = tool.extracted_obj.qualified_name if tool.extracted_obj else tool.name
            tools_section.append(f"  # - function: {qual_name}")
            tools_section.append(f"  #   name: {tool.name}")
            if tool.description:
                desc = tool.description.replace("\n", " ").replace('"', '\\"')
                tools_section.append(f'  #   description: "{desc[:60]}"')
            tools_section.append("  #   expose: true")
            tools_section.append("  #   tests:")
            tools_section.append("  #     - params:")
            sig = inspect.signature(tool.fn)
            params = [p for p in sig.parameters.values() if p.name != "self"]
            if params:
                for p in params:
                    tools_section.append(f"  #         {p.name}: null")
            else:
                tools_section.append("  #         {}")
            tools_section.append("  #       expect: null")
            tools_section.append("")



    if discovered_tools:
        tools_yaml = "tools:\n" + "\n".join(tools_section)
    else:
        tools_yaml = (
            "# No tools discovered yet. Add tools using the standalone @tool decorator\n"
            "# (from smarter_mcp import tool) or define functions in scanned files,\n"
            "# then re-run 'smarter-mcp init --force' to regenerate.\n"
            "#\n"
            "# Example once tools are present:\n"
            "#   tools:\n"
            "#     - function: mymodule.my_func\n"
            "#       name: custom_name\n"
            "#       tests:\n"
            "#         - params:\n"
            "#             arg1: null\n"
            "#           expect: null\n"
            "tools: []"
        )

    yaml_content = f"""# Smarter-MCP Server Manifest Configuration
# Feel free to edit this file to configure your server.

name: "{server_name}"
version: "0.1.0"
description: "MCP Server generated from {server_name}"

server:
  host: "0.0.0.0"
  port: 8000
  transport: "sse" # Options: sse, streamable-http, stdio
  log_level: "info"

sources:
  - path: "{source_path_for_manifest}"
    exclude:
      - "test_*"
      - "*_test.py"
      - "conftest.py"

# routing:
#   separator: "_"
#   overrides:
#     db/client: database

# expose:
#   include_private: false
#   include_dunder: false
#   include_inherited: false
#   include_properties: true
#   variadic_policy: "warn"
#   unannotated_policy: "expose"

# instances:
#   - class_name: "DatabaseClient"
#     lifecycle: "session"
#     constructor_args:
#       connection_string: "${{DB_URL:sqlite:///:memory:}}"

{tools_yaml}
"""

    try:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(yaml_content, encoding="utf-8")
        click.echo(click.style(f"✓ Initialized Smarter-MCP manifest at '{output_file}'", fg="green", bold=True))
    except Exception as e:
        raise click.ClickException(f"Failed to write manifest file: {e}")


@cli.command()
@click.option("--output", "-o", default="./dist", help="Output directory.")
@click.option("--package-name", "-n", help="Name of the generated package.")
@click.option("--manifest", "-m", help="Path to manifest YAML file.")
def export(output: str, package_name: str | None, manifest: str | None):
    """Export the server as a standalone pip package (coming soon)."""
    click.echo(click.style("Coming soon! The export command will be implemented in a future release.", fg="yellow", bold=True))
