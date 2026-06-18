from __future__ import annotations

import argparse
import logging
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .app_logging import configure_logging, current_log_path, log_destination_label, tail_log_lines
from .config import (
    ConfigError,
    ensure_config_saved,
    load_config,
    login,
    runtime_paths,
    set_frequency,
    set_kill_switch,
)
from .credits import spendable_balance, sync_local_wallet
from .metrics import (
    CLICK_EVENT,
    QP_EVENT,
    RENDERED_EVENT,
    VSCODE_WAIT_SURFACE,
    metric_contract,
)
from .privacy import public_event_schema
from .runner import CommandRunner
from .wallet import Wallet, WalletError


# How long maybe_start_gateway() waits for a freshly-spawned gateway to answer
# /healthz before warning the user. The wait loop returns the instant the
# gateway is healthy, so this is a cap, not a fixed delay -- it only elapses in
# full on a genuine failure to start. The old 0.15s was hopelessly short: a cold
# gateway needs ~0.3s even from source and seconds as a frozen binary, so it
# reported a false "did not start" on essentially every cold `sai claude`.
# Keep roughly in sync with gateway.py:start_gateway_in_background's default.
GATEWAY_START_WAIT_SECONDS = 8.0
logger = logging.getLogger(__name__)


def _is_windows() -> bool:
    # Indirection so tests can exercise the Windows-only terminal-font branch
    # without patching the global os.name (which would make pathlib raise on
    # POSIX CI runners).
    return os.name == "nt"


def default_backend_db_path() -> Path:
    return runtime_paths().home / "backend.sqlite3"


def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(f"{__package__}.{name}") is not None


def _backend_available() -> bool:
    """True when the server-only ``sai.backend`` module is present. It ships in
    the source/private build but is stripped from the public client package, so
    this gates whether the ``sai backend`` subcommands exist at all -- the frozen
    client binary then neither bundles the sponsor server nor advertises a
    command it cannot run."""
    return _module_available("backend")


def _dev_mock_available() -> bool:
    # The mock lab intentionally depends on the private/source backend. The
    # public client package strips both modules, so do not advertise this command
    # unless the full development checkout is present.
    return _backend_available() and _module_available("dev_mock")


def gateway_running(host: str = "127.0.0.1", port: int = 8787, timeout: float = 0.2) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            request = (
                f"GET /healthz HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Connection: close\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            # Read until the server closes (we asked for Connection: close).
            # A single recv(256) used to truncate the response: the /healthz
            # body ({"status": "ok"}) sits ~530 bytes in, after the security
            # headers, so the `"ok"` check never matched and a healthy gateway
            # was reported as down -- which made the agent wrapper spawn a
            # duplicate and print "did not start" even when one was serving.
            chunks: list[bytes] = []
            total = 0
            while total < 8192:  # the healthz response is tiny; cap to stay bounded
                chunk = sock.recv(1024)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            response = b"".join(chunks)
    except OSError:
        return False
    first_line = response.splitlines()[0] if response else b""
    return b" 200 " in first_line and b'"ok"' in response


def _gateway_child_env() -> dict[str, str]:
    """Environment for a detached gateway child: a copy of ours with the
    PyInstaller onefile marker removed so the child does not reuse (and get
    orphaned by) this process's extraction dir. No-op when not frozen."""
    env = dict(os.environ)
    env.pop("_MEIPASS2", None)
    return env


def start_gateway_in_background(
    host: str = "127.0.0.1",
    port: int = 8787,
    wait_seconds: float = GATEWAY_START_WAIT_SECONDS,
) -> bool:
    import subprocess
    import time

    if getattr(sys, "frozen", False):
        command = [sys.executable, "gateway", "serve", "--host", host, "--port", str(port)]
        mode = "frozen"
    else:
        command = [sys.executable, "-m", "sai", "gateway", "serve", "--host", host, "--port", str(port)]
        mode = "module"
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        # Give the detached gateway its own runtime. A PyInstaller onefile parent
        # exports _MEIPASS2 pointing at its private extraction dir; a child
        # launched from sys.executable would inherit it, skip its own extraction,
        # and bind to the parent's dir -- which is wiped when this `sai claude`
        # process exits, orphaning the still-running gateway. Dropping it lets the
        # gateway unpack (onefile) or resolve (onedir) its own runtime and survive.
        "env": _gateway_child_env(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    logger.info("Gateway autostart attempt host=%s port=%s mode=%s", host, port, mode)
    started = time.monotonic()
    try:
        subprocess.Popen(command, **kwargs)
    except OSError:
        logger.exception("Failed to start gateway process in background")
        return False
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while time.monotonic() < deadline:
        if gateway_running(host, port):
            logger.info("Gateway autostart ready host=%s port=%s elapsed_ms=%s", host, port, int((time.monotonic() - started) * 1000))
            return True
        time.sleep(0.05)
    logger.warning("Gateway autostart unhealthy host=%s port=%s elapsed_ms=%s", host, port, int((time.monotonic() - started) * 1000))
    return False


def provider_catalog() -> list[dict]:
    from .gateway import provider_catalog as _provider_catalog

    return _provider_catalog()


def serve_gateway(host: str = "127.0.0.1", port: int = 8787, open_browser: bool = False) -> None:
    from .gateway import serve_gateway as _serve_gateway

    _serve_gateway(host=host, port=port, open_browser=open_browser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sai", description="Sponsored AI Credits CLI")
    parser.add_argument("--version", action="version", version=f"sai {__version__}")
    sub = parser.add_subparsers(dest="command_name")

    login_cmd = sub.add_parser("login", help="Create or refresh a local SAI login")
    login_cmd.add_argument("--email", help="Optional email hint stored locally")
    login_cmd.add_argument("--name", help="Optional display name stored locally")

    wallet_cmd = sub.add_parser("wallet", help="Show wallet balance and recent ledger entries")
    wallet_cmd.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    wallet_cmd.add_argument(
        "--no-sync",
        action="store_true",
        help="Skip reconciling the local ledger against the backend before showing it",
    )

    placement_cmd = sub.add_parser(
        "placement",
        help="Fetch or report a sponsor placement for an external surface (e.g. the VS Code webview)",
    )
    placement_sub = placement_cmd.add_subparsers(dest="placement_action", required=True)
    placement_next = placement_sub.add_parser(
        "next", help="Fetch the next placement and record its rendered event (JSON)"
    )
    placement_next.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    placement_next.add_argument(
        "--surface", default=VSCODE_WAIT_SURFACE, help="Surface label issued server-side (default: vscode_ai_wait)"
    )
    placement_next.add_argument("--tool", default="claude", help="Agent the wait belongs to (claude/codex)")
    placement_next.add_argument(
        "--attended",
        action="store_true",
        help="Attest the user is attending (VS Code focused with recent input)",
    )
    placement_event = placement_sub.add_parser(
        "event", help="Record a placement event; reads the placement ticket JSON from stdin"
    )
    placement_event.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    placement_event.add_argument(
        "--event",
        default=QP_EVENT,
        choices=[RENDERED_EVENT, QP_EVENT, CLICK_EVENT],
        help="Event type (default: qualified_5s)",
    )
    placement_event.add_argument(
        "--visible-seconds", type=float, default=0.0, help="Seconds the card was continuously visible"
    )
    placement_event.add_argument(
        "--attended",
        action="store_true",
        help="Attest the user is attending at event time (focused with recent input)",
    )

    run_cmd = sub.add_parser("run", help="Run a command through SAI")
    run_cmd.add_argument("command", nargs=argparse.REMAINDER, help="Command after --")

    codex_cmd = sub.add_parser("codex", help="Run codex through SAI")
    codex_cmd.add_argument("args", nargs=argparse.REMAINDER)

    claude_cmd = sub.add_parser("claude", help="Run claude through SAI")
    claude_cmd.add_argument("args", nargs=argparse.REMAINDER)

    config_cmd = sub.add_parser("config", help="Show or update local settings")
    config_sub = config_cmd.add_subparsers(dest="config_command")
    config_sub.add_parser("show", help="Show config")
    set_cmd = config_sub.add_parser("set", help="Set config values")
    set_cmd.add_argument("key", choices=["frequency", "backend-url"])
    set_cmd.add_argument("value")
    kill_cmd = config_sub.add_parser("kill-switch", help="Toggle local kill switch")
    kill_cmd.add_argument("state", choices=["on", "off"])
    kill_cmd.add_argument("--reason")

    dashboard_cmd = sub.add_parser("dashboard", help="Serve the local wallet dashboard and open it in a browser")
    dashboard_cmd.add_argument("--host", default="127.0.0.1")
    dashboard_cmd.add_argument("--port", type=int, default=8787)
    dashboard_cmd.add_argument("--no-open", action="store_true", help="Do not open the browser")

    link_cmd = sub.add_parser(
        "link",
        help="Link this installation to your sponsoredai.dev account to track earnings on the web dashboard",
    )
    link_cmd.add_argument("--open", action="store_true", help="Open the dashboard in a browser")
    link_cmd.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    gateway_cmd = sub.add_parser("gateway", help="Run or inspect the OpenAI-compatible gateway")
    gateway_sub = gateway_cmd.add_subparsers(dest="gateway_command")
    serve_cmd = gateway_sub.add_parser("serve", help="Start the local gateway")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8787)
    gateway_sub.add_parser("key", help="Print the SAI API key")
    providers_cmd = gateway_sub.add_parser("providers", help="List built-in upstream provider presets")
    providers_cmd.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    if _dev_mock_available():
        dev_cmd = sub.add_parser("dev", help="Development-only mock surfaces")
        dev_sub = dev_cmd.add_subparsers(dest="dev_command")
        mock_cmd = dev_sub.add_parser("mock", help="Run a local full-product mock lab")
        mock_cmd.add_argument("--host", default="127.0.0.1")
        mock_cmd.add_argument("--backend-port", type=int, default=8790)
        mock_cmd.add_argument("--gateway-port", type=int, default=8787)
        mock_cmd.add_argument("--lab-port", type=int, default=8799)
        mock_cmd.add_argument("--home", type=Path, default=Path(".sai-mock"))
        mock_cmd.add_argument("--wait-seconds", type=float, default=8.0)
        mock_cmd.add_argument("--open", action="store_true", help="Open the mock lab in a browser")

    fonts_cmd = sub.add_parser("fonts", help="Detect or install a Nerd Font for sponsor card icons")
    fonts_sub = fonts_cmd.add_subparsers(dest="fonts_command")
    fonts_sub.add_parser("status", help="Show whether sponsor card icons will render")
    fonts_install = fonts_sub.add_parser("install", help="Install CaskaydiaMono Nerd Font for the current user")
    fonts_install.add_argument(
        "--no-terminal-config",
        action="store_true",
        help="Install the font but do not touch Windows Terminal settings",
    )
    fonts_install.add_argument(
        "--auto",
        action="store_true",
        help="Best-effort install for package hooks: skips CI and opted-out "
        "installs, never overrides a font the user already configured",
    )

    overlay_cmd = sub.add_parser(
        "overlay", help="Float the billable sponsor banner over Claude Desktop or the Codex app"
    )
    overlay_cmd.add_argument(
        "target",
        nargs="?",
        choices=["claude", "codex", "both", "mock"],
        default="claude",
        help="Desktop app to watch (default: claude)",
    )
    overlay_cmd.add_argument(
        "--target",
        dest="target_option",
        choices=["claude", "codex", "both", "mock"],
        help="Deprecated; use `sai overlay codex|claude|both`.",
    )
    overlay_cmd.add_argument(
        "--anchor",
        choices=["top", "bottom", "top-left", "top-right", "bottom-left", "bottom-right"],
        default="top",
        help="Where the banner sits relative to the app window (default top, clear of the composer)",
    )
    overlay_cmd.add_argument(
        "--bill",
        action="store_true",
        default=True,
        help="Earn real credits where the backend supports the desktop_overlay surface (default)",
    )
    overlay_cmd.add_argument(
        "--no-bill",
        dest="bill",
        action="store_false",
        help="Run a credit-0 preview without backend billing",
    )

    privacy_cmd = sub.add_parser("privacy", help="Inspect privacy guarantees")
    privacy_sub = privacy_cmd.add_subparsers(dest="privacy_command")
    privacy_sub.add_parser("schema", help="Print public event schema")

    logs_cmd = sub.add_parser("logs", help="Inspect local SAI application logs")
    logs_sub = logs_cmd.add_subparsers(dest="logs_command")
    logs_sub.add_parser("path", help="Print the active log file path")
    logs_tail = logs_sub.add_parser("tail", help="Print the last lines of the active log file")
    logs_tail.add_argument("--lines", type=int, default=80, help="Number of lines to print")

    # Server-only: present in the source/private build, stripped from the public
    # client. Register its subcommands only when sai.backend is importable.
    if _backend_available():
        _register_backend_parser(sub)

    return parser


def _register_backend_parser(sub) -> None:
    """Register the server-only ``sai backend`` subcommands (source build only)."""
    backend_cmd = sub.add_parser("backend", help="Run or inspect the sponsor backend")
    backend_sub = backend_cmd.add_subparsers(dest="backend_command")
    backend_serve = backend_sub.add_parser("serve", help="Start the local sponsor backend")
    backend_serve.add_argument("--host", default="127.0.0.1")
    backend_serve.add_argument("--port", type=int, default=8790)
    backend_serve.add_argument("--db", type=Path, default=default_backend_db_path())
    backend_serve.add_argument("--seed", action="store_true", help="Seed launch sample campaigns before serving")
    backend_seed = backend_sub.add_parser("seed", help="Seed launch sample campaigns")
    backend_seed.add_argument("--db", type=Path, default=default_backend_db_path())
    backend_market = backend_sub.add_parser("market", help="Print the public campaign marketplace")
    backend_market.add_argument("--db", type=Path, default=default_backend_db_path())
    backend_market.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    backend_migrate = backend_sub.add_parser("migrate", help="Initialize or migrate the backend database")
    backend_migrate.add_argument("--db", type=Path, default=default_backend_db_path())
    backend_backup = backend_sub.add_parser("backup", help="Create a consistent SQLite backup")
    backend_backup.add_argument("--db", type=Path, default=default_backend_db_path())
    backend_backup.add_argument("--output", type=Path, required=True, help="Backup file or destination directory")
    backend_sub.add_parser("contract", help="Print the QP metric contract")


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    configure_logging(service=logging_service(raw_argv))
    try:
        return _main(raw_argv)
    except (ConfigError, WalletError) as exc:
        logger.warning("command failed error=%s", type(exc).__name__)
        print(f"sai: {exc}", file=sys.stderr)
        return 1


def _main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    if raw_argv in (["--version"], ["-V"]):
        print(f"sai {__version__}")
        return 0
    if raw_argv and raw_argv[0] in {"run", "codex", "claude"}:
        return handle_passthrough(raw_argv)

    parser = build_parser()
    args = parser.parse_args(raw_argv)

    if args.command_name == "login":
        config = login(email=args.email, name=args.name)
        print("Logged in locally.")
        print(f"User: {config['user_id']}")
        print(f"SAI_API_KEY={config['api_key']}")
        return 0

    if args.command_name == "wallet":
        from .update_check import check_for_update, update_notice

        config = ensure_config_saved()
        wallet = Wallet()
        # Reconcile the local display ledger against the authoritative backend
        # balance before reading it, so spend and clawbacks (which never touched
        # the local ledger) are reflected. Best-effort: a missing/unreachable
        # backend leaves the local figures as-is.
        summary = sync_local_wallet(config=config, wallet=wallet) if not args.no_sync else None
        entries = wallet.entries()[-8:]
        # Passive update check (cached, best-effort, never blocks or raises): the
        # CLI has no auto-update, so a newer published version is surfaced here.
        # The VS Code status bar reads this same JSON, so the extension learns of
        # an update from the wallet read without its own npm probe.
        update_info = check_for_update()
        payload = {
            "balance": wallet.balance(),
            "recent_entries": entries,
            "local_wallet_authoritative": False,
            "gateway_spends_wallet": False,
            "backend_confirmed": summary is not None,
            "backend": summary,
            "update": {
                "available": update_info is not None,
                "current": __version__,
                "latest": update_info.latest if update_info else None,
            },
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Local display balance: {payload['balance']:.3f} AI credits")
            if summary is not None:
                print(
                    "Backend confirmed: "
                    f"spendable {spendable_balance(summary):.3f} "
                    f"(available {summary.get('available_balance', 0):.3f} + "
                    f"settled {summary.get('settled_balance', 0):.3f}), "
                    f"pending {summary.get('pending_balance', 0):.3f}, "
                    f"revoked {summary.get('revoked_balance', 0):.3f} AI credits"
                )
            else:
                print("Backend balance not confirmed (offline or install not yet registered).")
            print("Backend ledger is authoritative for earnings, spend, and payout.")
            if entries:
                print("Recent ledger:")
                for entry in entries:
                    print(
                        f"  {entry['timestamp']} {entry['kind']:>5} "
                        f"{entry['amount']:+.3f} {entry['source']}"
                    )
            if update_info is not None:
                print(update_notice(update_info), file=sys.stderr)
        return 0

    if args.command_name == "placement":
        from .sponsors import fetch_placement_card, record_placement_event

        config = ensure_config_saved()
        if args.placement_action == "next":
            result = fetch_placement_card(
                config, tool=args.tool, surface=args.surface, attended=args.attended
            )
        else:
            try:
                ticket = json.load(sys.stdin)
            except (json.JSONDecodeError, ValueError):
                ticket = {}
            if isinstance(ticket, dict) and isinstance(ticket.get("placement"), dict):
                ticket = ticket["placement"]
            elif not isinstance(ticket, dict):
                ticket = {}
            result = record_placement_event(
                config,
                ticket,
                event=args.event,
                visible_seconds=args.visible_seconds,
                attended=args.attended,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command_name == "config":
        return handle_config(args)

    if args.command_name == "dashboard":
        config = ensure_config_saved()
        if not config.get("api_key"):
            login()
        serve_gateway(host=args.host, port=args.port, open_browser=not args.no_open)
        return 0

    if args.command_name == "link":
        ensure_config_saved()
        from .gateway import start_install_link

        result = start_install_link()
        if result is None:
            print(
                "Could not get a pairing code. Run an agent once (for example `sai claude`) so "
                "this installation registers with the backend, then try `sai link` again.",
                file=sys.stderr,
            )
            return 1
        code = str(result.get("code") or "")
        dashboard_url = str(result.get("dashboard_url") or "")
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            pretty = f"{code[:4]}-{code[4:]}" if len(code) == 8 else code
            minutes = max(1, int(result.get("expires_in_seconds") or 0) // 60)
            print(f"Pairing code: {pretty}")
            print(f"Open {dashboard_url}, sign in, and enter the code to link this installation.")
            print(f"The code expires in {minutes} minutes and can be used once.")
        if getattr(args, "open", False) and dashboard_url:
            # dashboard_url is backend-supplied; only ever launch http/https.
            from .browser import open_url

            open_url(dashboard_url)
        return 0

    if args.command_name == "gateway":
        return handle_gateway(args)

    if args.command_name == "dev":
        return handle_dev(args, parser)

    if args.command_name == "fonts":
        return handle_fonts(args, parser)

    if args.command_name == "overlay":
        from .overlay.app import run_overlay

        target = args.target_option or args.target
        return run_overlay(target=target, anchor=args.anchor, billable=(args.bill and target != "mock"))

    if args.command_name == "privacy":
        if args.privacy_command == "schema":
            print(json.dumps(public_event_schema(), indent=2, sort_keys=True))
            return 0
        parser.error("privacy requires a subcommand")

    if args.command_name == "logs":
        return handle_logs(args, parser)

    if args.command_name == "backend":
        return handle_backend(args)

    parser.print_help()
    return 2


def handle_passthrough(raw_argv: Sequence[str]) -> int:
    command_name = raw_argv[0]
    raw_rest = list(raw_argv[1:])
    rest = normalize_remainder(raw_rest)

    if command_name == "run":
        # Only treat --help as ours when it comes before the -- separator;
        # `sai run -- --help` is a literal command for the child process.
        if raw_rest and raw_rest[0] in {"-h", "--help"}:
            print("usage: sai run [--] <command> [args...]")
            print("Run a command through SAI; sponsor cards may appear during long waits.")
            return 0
        if not rest:
            print("sai run requires a command after --", file=sys.stderr)
            return 2

    config = ensure_config_saved()

    if command_name == "run":
        command = rest
        tool = detect_tool(rest)
    else:
        command = [command_name, *rest]
        tool = command_name

    maybe_start_gateway(tool)
    receipt = CommandRunner(config).run(command, tool=tool)
    # The agent has exited and the terminal is back to normal flow, so a one-line
    # update nudge here cannot clobber a repainting viewport (it would mid-session).
    # TTY-gated and cached, so it stays silent in pipes/CI and costs nothing once
    # the daily check has run.
    from .update_check import notify_terminal_update

    notify_terminal_update()
    return receipt.exit_code


GATEWAY_AUTOSTART_TOOLS = {"claude", "codex"}


def maybe_start_gateway(tool: str) -> None:
    """Agent wrappers can use the local OpenAI-compatible gateway, so make sure
    one is listening before the agent starts. Opt out with SAI_NO_AUTO_GATEWAY=1.
    A failure to start never blocks the agent."""
    if tool not in GATEWAY_AUTOSTART_TOOLS:
        return
    if os.environ.get("SAI_NO_AUTO_GATEWAY", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    if gateway_running():
        return
    if start_gateway_in_background():
        # start_gateway_in_background only returns True once /healthz answers, so
        # the gateway is actually ready (not merely spawned) by the time we print.
        print("SAI gateway ready in the background: http://127.0.0.1:8787/v1")
    else:
        logger.error("Gateway autostart failed", extra={"tool": tool})
        print(
            "sai: the local gateway did not start; run `sai gateway serve` in another terminal",
            file=sys.stderr,
        )


def handle_fonts(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from . import fonts

    if args.fonts_command == "status":
        print_fonts_status(fonts)
        return 0
    if args.fonts_command == "install":
        if args.auto:
            skip = font_autoinstall_skip_reason(fonts)
            if skip:
                print(f"sai: skipping font install: {skip}")
                return 0
        print(f"Downloading {fonts.FONT_ZIP_URL} ...")
        try:
            installed = fonts.install_font()
        except (OSError, RuntimeError) as exc:
            print(f"sai: font install failed: {exc}", file=sys.stderr)
            # Never fail a package install hook over an optional font.
            return 0 if args.auto else 1
        print(f"Installed {fonts.FONT_FAMILY}: {installed}")
        if _is_windows() and not args.no_terminal_config:
            if args.auto:
                # Never let a package hook silently repoint the user's terminal
                # font: a freshly installed per-user font is not visible to the
                # running session, so the terminal just errors on next open until
                # a restart. Install the file and let the user opt in explicitly.
                print(
                    f"Font installed. Set your terminal font to '{fonts.FONT_FAMILY}' "
                    "(or run `sai fonts install`), then restart the terminal, to see sponsor icons."
                )
            else:
                updated = fonts.configure_windows_terminal()
                if updated:
                    print(f"Windows Terminal default font set to {fonts.FONT_FAMILY}.")
                    print("Log off and back on (or restart Windows) for the per-user font to")
                    print("become visible, otherwise the terminal reports it as missing.")
                    print(f"Backup of previous settings: {updated}.sai-backup")
                else:
                    print(f"Could not update terminal settings; set your terminal font to '{fonts.FONT_FAMILY}'.")
        elif not _is_windows():
            # macOS and Linux resolve missing glyphs from any installed font
            # (CoreText / fontconfig), so installing is normally enough.
            print("Installed for the current user; the terminal picks it up via font fallback.")
        else:
            print(f"Set your terminal font to '{fonts.FONT_FAMILY}' to see sponsor card icons.")
        fonts.clear_caches()
        print_fonts_status(fonts)
        return 0
    parser.error("fonts requires a subcommand")
    return 2


def handle_dev(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.dev_command == "mock":
        from .dev_mock import run_mock_lab

        return run_mock_lab(
            host=args.host,
            backend_port=args.backend_port,
            gateway_port=args.gateway_port,
            lab_port=args.lab_port,
            home=args.home,
            wait_seconds=args.wait_seconds,
            open_browser=args.open,
        )
    parser.error("dev requires a subcommand")
    return 2


def font_autoinstall_skip_reason(fonts) -> str | None:
    from .config import ci_environment

    if os.environ.get("SAI_NO_FONT_INSTALL", "").strip().lower() in {"1", "true", "yes", "on"}:
        return "SAI_NO_FONT_INSTALL is set"
    if ci_environment():
        return "CI environment"
    if os.environ.get("SAI_ICONS", "auto").strip().lower() in {"0", "off", "false", "no"}:
        return "SAI_ICONS=off"
    if fonts.icons_enabled():
        return "icons already enabled"
    return None


def print_fonts_status(fonts) -> int:
    mode = os.environ.get("SAI_ICONS", "auto").strip().lower() or "auto"
    detected = fonts.nerd_font_available()
    enabled = fonts.icons_enabled()
    faces = fonts.windows_terminal_faces() + fonts.vscode_terminal_faces()
    print(f"Sponsor card icons: {'on' if enabled else 'off'} (SAI_ICONS={mode})")
    print(f"Nerd Font detected: {'yes' if detected else 'no'}")
    if faces:
        print("Configured terminal fonts: " + ", ".join(sorted(set(faces))))
    if not enabled:
        print("Run `sai fonts install` to install one, or set SAI_ICONS=on to force icons.")
    return 0


def handle_config(args: argparse.Namespace) -> int:
    if args.config_command == "show":
        config = ensure_config_saved()
        redacted = {
            key: redact(value) if (_is_secret_config_key(key) and isinstance(value, str) and value) else value
            for key, value in config.items()
        }
        print(json.dumps(redacted, indent=2, sort_keys=True))
        print(f"Config path: {runtime_paths().config_file}")
        return 0
    if args.config_command == "set":
        if args.key == "frequency":
            try:
                config = set_frequency(args.value)
            except ValueError as exc:
                print(f"sai: {exc}", file=sys.stderr)
                return 2
            print(f"frequency={config['frequency']}")
            return 0
        if args.key == "backend-url":
            config = ensure_config_saved()
            config["backend_url"] = None if args.value.lower() in {"none", "off", "null"} else args.value.rstrip("/")
            from .config import save_config

            save_config(config)
            print(f"backend_url={config['backend_url']}")
            return 0
    if args.config_command == "kill-switch":
        set_kill_switch(args.state == "on", reason=args.reason)
        print(f"kill_switch={args.state}")
        return 0
    raise SystemExit("config requires a subcommand")


def handle_gateway(args: argparse.Namespace) -> int:
    if args.gateway_command == "providers":
        providers = provider_catalog()
        if args.json:
            print(json.dumps({"providers": providers}, indent=2, sort_keys=True))
            return 0
        print("provider    key env             key     base URL")
        for provider in providers:
            marker = "*" if provider["selected"] else " "
            key_state = "set" if provider["key_set"] else "missing"
            print(
                f"{marker}{provider['name']:<11} {provider['api_key_env']:<19} "
                f"{key_state:<7} {provider['base_url']}"
            )
        print(
            "Select with SAI_GATEWAY_PROVIDER=<provider>. "
            "Custom override: SAI_UPSTREAM_BASE_URL + SAI_UPSTREAM_API_KEY."
        )
        return 0

    config = ensure_config_saved()
    if args.gateway_command == "serve":
        if not config.get("api_key"):
            config = login()
        serve_gateway(host=args.host, port=args.port)
        return 0
    if args.gateway_command == "key":
        if not config.get("api_key"):
            config = login()
        print(config["api_key"])
        return 0
    raise SystemExit("gateway requires a subcommand")


def handle_logs(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.logs_command == "path":
        print(log_destination_label())
        return 0
    if args.logs_command == "tail":
        path = current_log_path()
        if path is None:
            print("sai: file logging is disabled for this process", file=sys.stderr)
            return 1
        lines = tail_log_lines(args.lines)
        if not lines:
            print(f"No log entries yet: {path}")
            return 0
        for line in lines:
            print(line)
        return 0
    parser.error("logs requires a subcommand")
    return 2


def handle_backend(args: argparse.Namespace) -> int:
    try:
        from .backend import BackendStore, serve_backend
    except ImportError:
        print(
            "sai: the sponsor backend server is not bundled in this build "
            "(client-only). Install SAI from source to run `sai backend`.",
            file=sys.stderr,
        )
        return 2

    if args.backend_command == "serve":
        serve_backend(host=args.host, port=args.port, db_path=args.db, seed=args.seed)
        return 0
    if args.backend_command == "seed":
        store = BackendStore(args.db)
        store.seed_demo()
        print(f"Seeded launch sample campaigns in {args.db}")
        return 0
    if args.backend_command == "market":
        store = BackendStore(args.db)
        payload = store.market()
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print("SAI market")
        print("rank  campaign                  bid/block  dev/QP  delivered  spend   status")
        for row in payload["rows"]:
            print(
                f"{row['rank']:>4}  {row['campaign'][:24]:<24}  "
                f"{row['bid_per_1000_qp']:>9.2f}  {row['developer_payout']:>6.3f}  {row['delivered']:>9}  "
                f"{row['spend']:>6.3f}  {row['status']}"
            )
        return 0
    if args.backend_command == "migrate":
        BackendStore(args.db)
        print(f"Migrated backend database at {args.db}")
        return 0
    if args.backend_command == "backup":
        store = BackendStore(args.db)
        backup_path = store.backup(args.output)
        print(f"Backed up backend database to {backup_path}")
        return 0
    if args.backend_command == "contract":
        print(json.dumps(metric_contract(), indent=2, sort_keys=True))
        return 0
    raise SystemExit("backend requires a subcommand")


def logging_service(argv: Sequence[str]) -> str:
    if not argv:
        return "cli"
    if argv[0] in {"backend", "gateway"} and len(argv) > 1 and argv[1] == "serve":
        return argv[0]
    return "cli"


def normalize_remainder(values: Sequence[str]) -> list[str]:
    values = list(values)
    if values and values[0] == "--":
        return values[1:]
    return values


def detect_tool(command: Sequence[str]) -> str:
    executable = command[0].lower().replace("\\", "/").split("/")[-1]
    for extension in (".exe", ".cmd", ".bat", ".ps1"):
        if executable.endswith(extension):
            executable = executable[: -len(extension)]
            break
    if executable in {"codex", "claude", "npm", "pnpm", "pytest", "cargo", "go", "docker", "terraform"}:
        return executable
    return "run"


def redact(value: str) -> str:
    if len(value) <= 10:
        return "***"
    return f"{value[:6]}...{value[-4:]}"


# Config keys whose values are secrets and must never be printed in clear.
# install_id is the seed the per-install auth secret is derived from, so it is
# treated as a credential too.
_SECRET_CONFIG_KEYS = {"install_id", "spend_key_hash"}


def _is_secret_config_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SECRET_CONFIG_KEYS:
        return True
    return (
        lowered.endswith("_key")
        or lowered.endswith("_secret")
        or "token" in lowered
        or "api_key" in lowered
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
