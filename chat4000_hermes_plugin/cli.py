"""CLI subcommands: `chat4000 pair|status|reset|wizard|telemetry` (v2 / Matrix).

Pairing is now a 6-digit OTP via the registrar (protocol C), not the v1 relay
handshake:

  pair   — self-onboard the bot identity on first run, then register a 6-digit
           user code, show it (+ QR), poll until the device redeems, and record
           the paired user so the running gateway invites them.
  status — show the bot identity + paired users.
  reset  — wipe bot creds + crypto store + known users (destructive).
  wizard — interactive installer (unchanged).
  telemetry — opt in/out (unchanged).

Config (env): CHAT4000_REGISTRAR_URL (default prod), CHAT4000_SERVICE_TOKEN
(required for register/status — see pushback X2).
"""

from __future__ import annotations

import asyncio
import io
import os
import secrets
import sys

from .matrix.creds_store import crypto_store_path, load_bot_creds, save_bot_creds
from .matrix.registrar_client import RegistrarClient, RegistrarError
from .matrix.users_store import add_known_user, load_known_users
from .package_info import read_package_version
from .telemetry import (
    get_telemetry_status,
    initialize_chat4000_telemetry,
    set_telemetry_enabled,
)

APP_ID = "@chat4000/hermes-plugin"

# Per-environment registrar. The registrar you pair against decides everything:
# the stage registrar mints stage creds whose gateway_url points at the stage
# gateway, so the running plugin follows automatically (it just uses the stored
# creds). Select with `chat4000 pair --stage` or CHAT4000_ENV=stage.
REGISTRAR_URLS = {
    "production": "https://registrar.chat4000.com",
    "stage": "https://registrar.stgcht4.duckdns.org",
}


def _resolve_env() -> str:
    env = os.environ.get("CHAT4000_ENV", "").strip().lower()
    return env if env in REGISTRAR_URLS else "production"


def _resolve_registrar_url() -> str:
    # An explicit URL override wins (self-hosted / custom); else by environment.
    explicit = os.environ.get("CHAT4000_REGISTRAR_URL", "").strip()
    return explicit or REGISTRAR_URLS[_resolve_env()]


def _registrar() -> RegistrarClient:
    token = os.environ.get("CHAT4000_SERVICE_TOKEN", "").strip() or None
    return RegistrarClient(_resolve_registrar_url(), token)


def _gen_code() -> str:
    """A 6-digit CSPRNG OTP (always exactly 6 digits)."""
    return f"{secrets.randbelow(900000) + 100000:06d}"


def _build_chat4000_cli():
    import click  # type: ignore[import-not-found]

    @click.group(name="chat4000", help="Manage chat4000 (Matrix) onboarding and pairing")
    @click.option("--no-telemetry", is_flag=True, default=False,
                  help="Disable anonymous error reporting for this run.")
    @click.pass_context
    def chat4000(ctx_obj, no_telemetry: bool):
        if no_telemetry:
            os.environ["CHAT4000_TELEMETRY_DISABLED"] = "1"

    @chat4000.command("pair")
    @click.option("--account", default="default", help="Account id")
    @click.option("--stage", is_flag=True, default=False,
                  help="Pair against the stage servers (stgcht4.duckdns.org).")
    def cmd_pair(account, stage):
        """Onboard (first run) and pair a device with a 6-digit code."""
        if stage:
            os.environ["CHAT4000_ENV"] = "stage"
        try:
            asyncio.run(_run_pair(account))
        except KeyboardInterrupt:
            click.echo("\nPairing cancelled.")
        except Exception as exc:  # noqa: BLE001
            _handle_cli_error(exc)

    @chat4000.command("status")
    @click.option("--account", default="default", help="Account id")
    def cmd_status(account):
        """Show the bot identity and paired users."""
        import click as _c
        creds = load_bot_creds(account)
        users = load_known_users(account)
        if creds is None:
            _c.echo("configured: no  (run `chat4000 pair`)")
            return
        _c.echo("\n".join([
            f"account:     {account}",
            f"environment: {_resolve_env()}",
            f"bot user:    {creds.user_id}",
            f"device:      {creds.device_id}",
            f"gateway:     {creds.gateway_url}",
            f"plugin_id:   {creds.plugin_id or '(none)'}",
            f"paired users: {len(users)}" + (": " + ", ".join(users) if users else ""),
            "configured:  yes",
        ]))

    @chat4000.command("reset")
    @click.option("--account", default="default", help="Account id")
    def cmd_reset(account):
        """Wipe bot creds + crypto store + known users (destructive)."""
        _run_reset(account)

    @chat4000.command("wizard")
    def cmd_wizard():
        """Interactive install wizard."""
        from .install_wizard import main as wizard_main
        sys.exit(wizard_main())

    @chat4000.group("telemetry")
    def telemetry_group():
        """Manage anonymous error reporting."""

    @telemetry_group.command("status")
    def cmd_tel_status():
        import click as _c
        s = get_telemetry_status()
        _c.echo(f"Telemetry: {'enabled' if s['enabled'] else 'disabled'} ({s['reason']})")

    @telemetry_group.command("disable")
    def cmd_tel_disable():
        import click as _c
        from . import analytics
        analytics.track("telemetry_preference_changed", {"enabled": False})
        analytics.flush()
        analytics.shutdown()
        set_telemetry_enabled(False)
        _c.echo("Telemetry disabled.")

    @telemetry_group.command("enable")
    def cmd_tel_enable():
        import click as _c
        set_telemetry_enabled(True)
        from . import analytics
        analytics.initialize_chat4000_analytics()
        analytics.track("telemetry_preference_changed", {"enabled": True})
        analytics.flush()
        _c.echo("Telemetry enabled.")

    return chat4000


def register_chat4000_cli(ctx) -> None:
    ctx.register_cli(_build_chat4000_cli())


def main() -> None:
    import signal
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    initialize_chat4000_telemetry()
    cli = _build_chat4000_cli()
    try:
        cli(prog_name="chat4000")
    except BrokenPipeError:
        sys.exit(0)


# ─── pair flow ──────────────────────────────────────────────────────────────


async def _run_pair(account: str) -> None:
    import click  # type: ignore[import-not-found]

    version = read_package_version()
    reg = _registrar()
    click.echo(f"Environment: {_resolve_env()}  (registrar: {_resolve_registrar_url()})")

    # Version gate (C.5) — refuse on force_upgrade, report to the operator.
    try:
        verdict = await reg.version(APP_ID, version, "production")
        if verdict.action == "force_upgrade":
            click.echo(f"Update required (>= {verdict.recommended}). Aborting.", err=True)
            sys.exit(2)
        if verdict.action == "recommend_upgrade":
            click.echo(f"Note: a newer version is recommended ({verdict.recommended}).")
    except RegistrarError as exc:
        click.echo(f"(version check skipped: {exc})")

    # Self-onboard the bot identity on first run.
    creds = load_bot_creds(account)
    if creds is None:
        if not os.environ.get("CHAT4000_SERVICE_TOKEN", "").strip():
            raise RuntimeError(
                "CHAT4000_SERVICE_TOKEN is required to onboard (see pushback X2). "
                "Set it in the plugin's environment."
            )
        onboard_code = _gen_code()
        creds = await reg.self_onboard(onboard_code, device_name="hermes-plugin")
        save_bot_creds(creds, account)
        click.echo(f"Onboarded plugin identity: {creds.user_id}")
        click.echo(f"Bot creds saved (gateway: {creds.gateway_url}).")

    # Register a user pairing code.
    code = _gen_code()
    await reg.register(code, kind="user", plugin_id=creds.plugin_id)
    click.echo("")
    click.echo(f"  Pairing code:  {code[:3]} {code[3:]}")
    _render_qr_if_possible(f"chat4000://pair?code={code}")
    click.echo("Enter this code in the chat4000 app. Waiting for the device…")
    click.echo("(Ctrl-C to stop.)")

    user_id = await reg.poll_until_complete(code)
    if user_id is None:
        click.echo("Pairing code expired without a device redeeming it. Try again.")
        return

    add_known_user(user_id, account)
    click.echo("")
    click.echo(f"✓ Paired {user_id}.")
    click.echo("The running gateway will invite them + share keys on its next start.")
    click.echo("If the gateway is running, restart it to pick up the new pairing.")


def _run_reset(account: str) -> None:
    import click  # type: ignore[import-not-found]
    from pathlib import Path

    from .key_store import resolve_chat4000_plugin_dir

    plugin_dir = resolve_chat4000_plugin_dir()
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (account or "default"))
    targets = [
        plugin_dir / f"matrix-creds-{safe}.json",
        plugin_dir / f"known-users-{safe}.json",
        Path(crypto_store_path(account)),
        Path(crypto_store_path(account) + "-wal"),
        Path(crypto_store_path(account) + "-shm"),
    ]
    removed = []
    for p in targets:
        if p.exists():
            try:
                p.unlink()
                removed.append(str(p))
            except OSError:
                pass
    if not removed:
        click.echo(f'No chat4000 state for account "{account}".')
        return
    click.echo(f'Reset chat4000 account "{account}". Removed:')
    for p in removed:
        click.echo(f"  {p}")
    click.echo("Re-pair with: chat4000 pair")


# ─── helpers ─────────────────────────────────────────────────────────────────


def _render_qr_if_possible(payload: str) -> None:
    import click  # type: ignore[import-not-found]

    click.echo(f"QR payload: {payload}")
    if not sys.stdout.isatty():
        return
    try:
        import qrcode  # type: ignore[import-not-found]

        qr = qrcode.QRCode(border=1)
        qr.add_data(payload)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        click.echo(buf.getvalue())
    except Exception:
        pass


def _handle_cli_error(exc: BaseException) -> None:
    import click  # type: ignore[import-not-found]
    from .error_log import dump_chat4000_trace

    log_path = dump_chat4000_trace("cli", exc)
    click.echo(f"chat4000 error: {exc}", err=True)
    click.echo(f"Trace log: {log_path}", err=True)
