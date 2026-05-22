"""CLI subcommands: `hermes chat4000 setup|pair|status|reset|telemetry|sessions`.

Port of clawconnect-plugin/src/cli.ts. Registered via Hermes' ctx.register_cli()
which Hermes core plumbs into a top-level Click group named `chat4000`.

Reduced surface compared to the TS plugin — the user asked for one session
(no `sessions list/bind/clear/current` subcommands) and tool calls (no
new CLI surface; tool rendering is a Swift-app feature). We keep:

  setup     — first-time interactive: write config + start pairing
  pair      — start a fresh pairing session (host-side)
  pair-many — repeated pairing with a fixed code (App Store review mode)
  status    — show current state (configured, group_id prefix)
  reset     — destructive wipe of group key + ack store
  telemetry — opt in/out
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .accounts import resolve_chat4000_account
from .ack_store import cleanup_stale_ack_store_lock, resolve_ack_store_path
from .crypto import (
    PAIRING_CODE_ALPHABET,
    generate_pairing_code,
    normalize_pairing_code,
)


def _validate_user_supplied_code(raw: str) -> str:
    """Reject pairing codes with banned characters.

    The alphabet excludes 0, 1, 5, I, L, O, S to avoid the visual
    ambiguities (zero vs O, one vs I vs L, five vs S) that plague
    handwritten / typed transcription. Generated codes are 8 chars
    formatted as XXXX-YYYY; user-supplied codes must follow the
    same shape (4+dash+4) so they're consistent on both ends.

    Raises ClickException on invalid input — surfaces a clean error
    to the operator instead of silently normalizing and pairing with
    a different code than what they typed."""
    import click  # type: ignore[import-not-found]

    stripped = (raw or "").replace(" ", "").replace("-", "").upper()
    if not stripped:
        raise click.ClickException("Pairing code is empty.")

    bad = sorted({ch for ch in stripped if ch not in PAIRING_CODE_ALPHABET})
    if bad:
        raise click.ClickException(
            f"Pairing code {raw!r} contains illegal characters: "
            f"{''.join(bad)}.\n"
            f"Allowed alphabet (28 chars, no 0/1/5/I/L/O/S to avoid "
            f"visual ambiguity): {PAIRING_CODE_ALPHABET}"
        )

    if len(stripped) != 8:
        raise click.ClickException(
            f"Pairing code must be 8 characters from the alphabet "
            f"(formatted XXXX-YYYY). Got {len(stripped)}: {raw!r}"
        )

    # Re-format as XXXX-YYYY so display + room derivation are consistent.
    return f"{stripped[:4]}-{stripped[4:]}"
from .error_log import dump_chat4000_trace
from .key_store import (
    inspect_chat4000_state_access,
    resolve_chat4000_key_file_path,
    save_stored_group_key,
)
from .pairing import (
    PairHostOptions,
    PairHostStatus,
    host_pairing_session,
    host_pairing_session_continuous,
)
from .telemetry import (
    get_telemetry_status,
    initialize_chat4000_telemetry,
    set_telemetry_enabled,
)

# Pairing code alphabet box-glyph banner — used by both the CLI host
# screen and the optional QR fallback.
_PAIR_CODE_FONT = {
    # Trimmed for brevity vs the TS impl — the CLI shows the code in plain
    # text and a QR code (when terminal supports unicode); the ASCII-art
    # banner is a nice-to-have we skip for v1 to keep this file smaller.
}


def _build_chat4000_cli():
    """Build the Click `chat4000` subgroup. Module-level factory so the
    same group can be registered with Hermes (`ctx.register_cli`) AND
    served as a standalone console script (`chat4000 ...` after
    `pip install`)."""

    import click  # type: ignore[import-not-found]

    @click.group(
        name="chat4000",
        help="Manage chat4000 pairing and local key state",
    )
    @click.option(
        "--no-telemetry",
        is_flag=True,
        default=False,
        help="Disable anonymous error reporting for this run.",
    )
    @click.pass_context
    def chat4000(ctx_obj, no_telemetry: bool):
        if no_telemetry:
            os.environ["CHAT4000_TELEMETRY_DISABLED"] = "1"

    @chat4000.command("setup")
    @click.option("--account", default="default", help="Account id")
    @click.option(
        "--pairing-log-level",
        type=click.Choice(["info", "debug"]),
        default=None,
    )
    @click.option(
        "--runtime-log-level",
        type=click.Choice(["info", "debug"]),
        default=None,
    )
    @click.option(
        "--no-pair", is_flag=True, default=False,
        help="Save config and local key without starting pairing."
    )
    def cmd_setup(account, pairing_log_level, runtime_log_level, no_pair):
        """Interactive first-time setup and pairing."""
        try:
            asyncio.run(
                _run_interactive_setup(
                    account_id=account,
                    pairing_log_level=pairing_log_level,
                    runtime_log_level=runtime_log_level,
                    skip_pairing=no_pair,
                )
            )
        except KeyboardInterrupt:
            click.echo("\nSetup cancelled.")
        except Exception as exc:
            _handle_cli_error(exc)

    @chat4000.command("pair")
    @click.option("--account", default="default", help="Account id")
    @click.option("--code", default=None, help="Use this pairing code instead of a random one.")
    @click.option(
        "--pairing-log-level",
        type=click.Choice(["info", "debug"]),
        default=None,
    )
    def cmd_pair(account, code, pairing_log_level):
        """Start a new pairing session for another client."""
        try:
            asyncio.run(
                _run_pair_command(
                    account_id=account,
                    code=code,
                    pairing_log_level=pairing_log_level,
                )
            )
        except KeyboardInterrupt:
            click.echo("\nPairing cancelled.")
        except Exception as exc:
            _handle_cli_error(exc)

    @chat4000.command("pair-many")
    @click.option("--account", default="default", help="Account id")
    @click.option("--code", required=True, help="Fixed pairing code (reused across pairings).")
    @click.option("--max", "max_pairings", default=None, type=int, help="Stop after N pairings.")
    @click.option("--delay-ms", default=1000, type=int, help="Delay between pairings (ms).")
    @click.option(
        "--pairing-log-level",
        type=click.Choice(["info", "debug"]),
        default=None,
    )
    def cmd_pair_many(account, code, max_pairings, delay_ms, pairing_log_level):
        """Continuously pair multiple devices with the same fixed code
        (e.g. App Store review mode)."""
        try:
            asyncio.run(
                _run_pair_many_command(
                    account_id=account,
                    code=code,
                    max_pairings=max_pairings,
                    delay_ms=delay_ms,
                    pairing_log_level=pairing_log_level,
                )
            )
        except KeyboardInterrupt:
            click.echo("\nContinuous pairing stopped.")
        except Exception as exc:
            _handle_cli_error(exc)

    @chat4000.command("status")
    @click.option("--account", default="default", help="Account id")
    def cmd_status(account):
        """Show current chat4000 channel status."""
        try:
            cfg = _load_hermes_config()
            acct = resolve_chat4000_account(cfg, account)
            lines = [
                f"account: {acct.account_id}",
                f"pairing log level: {acct.pairing_log_level}",
                f"runtime log level: {acct.runtime_log_level}",
                f"key source: {acct.key_source}",
                f"key file: {acct.key_file_path}",
                f"group id: {acct.group_id or '(missing)'}",
                f"configured: {'yes' if acct.configured else 'no'}",
            ]
            click.echo("\n".join(lines))
        except Exception as exc:
            _handle_cli_error(exc)

    @chat4000.command("reset")
    @click.option("--account", default="default", help="Account id")
    def cmd_reset(account):
        """Wipe local key + ack store for an account. Destructive, no
        confirm. Re-pair after."""
        try:
            _run_reset_command(account)
        except Exception as exc:
            _handle_cli_error(exc)

    # ─── Telemetry subgroup ───────────────────────────────────────────────

    @chat4000.group("telemetry")
    def telemetry_group():
        """Manage anonymous error reporting."""

    @telemetry_group.command("status")
    def cmd_tel_status():
        s = get_telemetry_status()
        click.echo(f"Telemetry: {'enabled' if s['enabled'] else 'disabled'}")
        click.echo(f"  Reason: {s['reason']}")
        if s["enabled"]:
            click.echo("  Disable: hermes chat4000 telemetry disable")
            click.echo("  Or set CHAT4000_TELEMETRY_DISABLED=1")
        else:
            click.echo("  Enable:  hermes chat4000 telemetry enable")

    @telemetry_group.command("disable")
    def cmd_tel_disable():
        set_telemetry_enabled(False)
        click.echo("Telemetry disabled. No data will be sent to chat4000.")
        click.echo("Re-enable: hermes chat4000 telemetry enable")

    @telemetry_group.command("enable")
    def cmd_tel_enable():
        set_telemetry_enabled(True)
        click.echo("Telemetry enabled. Anonymous error reports will be sent.")
        click.echo("Privacy policy: https://chat4000.com/privacy")

    return chat4000


def register_chat4000_cli(ctx) -> None:
    """Wire the chat4000 subgroup into Hermes' click app via ctx.register_cli.
    Only used by Hermes versions that expose register_cli on PluginContext."""
    ctx.register_cli(_build_chat4000_cli())


def main() -> None:
    """Standalone entry point — `chat4000 ...` after `pip install`.

    Used when Hermes doesn't expose ctx.register_cli (current v0.14.0) or
    when running outside Hermes' process entirely (devops scripts, App
    Store review pair-many, etc.). Wires up telemetry before dispatch."""
    initialize_chat4000_telemetry()
    cli = _build_chat4000_cli()
    cli(prog_name="chat4000")


# ─── Command implementations (async-capable to share pairing logic) ───────


async def _run_interactive_setup(
    *,
    account_id: str,
    pairing_log_level: Optional[str],
    runtime_log_level: Optional[str],
    skip_pairing: bool,
) -> None:
    import click  # type: ignore[import-not-found]

    cfg = _load_hermes_config()
    acct = resolve_chat4000_account(cfg, account_id)
    pll = _normalize_log_level(pairing_log_level or acct.pairing_log_level)
    rll = _normalize_log_level(runtime_log_level or acct.runtime_log_level)

    # Ensure Hermes' config.yaml lists chat4000 in plugins.enabled. Pip-
    # installed plugins are discovered via entry-points but only loaded
    # when explicitly enabled in config — this auto-enable line means
    # the install runbook stays 3 commands without a separate edit step.
    _ensure_plugin_enabled_in_hermes_config()

    # Persist channel config back into Hermes' config.yaml via core's
    # writer. Hermes core exposes this on the ctx — but inside a CLI
    # command we don't have ctx, so we just mint the key file and let
    # the user enable the platform via env var on next gateway start.
    # (Same shortcut the TS impl takes when running outside the gateway.)
    group_key_bytes = _ensure_local_key_for_account(acct)
    click.echo("Saved chat4000 settings.")

    if skip_pairing:
        click.echo('Skipped pairing.\nNext: "hermes chat4000 pair"')
        return
    await _run_pair_command(
        account_id=acct.account_id,
        code=None,
        pairing_log_level=pll,
    )


async def _run_pair_command(
    *,
    account_id: str,
    code: Optional[str],
    pairing_log_level: Optional[str],
) -> bool:
    import click  # type: ignore[import-not-found]

    _ensure_plugin_enabled_in_hermes_config()
    cfg = _load_hermes_config()
    acct = resolve_chat4000_account(cfg, account_id)
    pll = _normalize_log_level(pairing_log_level or acct.pairing_log_level)
    raw = (code or "").strip()
    code = _validate_user_supplied_code(raw) if raw else generate_pairing_code()
    group_key_bytes = _ensure_local_key_for_account(acct)

    # (Re)start the gateway in the background BEFORE blocking on iPhone
    # scan. This is the side-effect that lets the install be just two
    # commands (`pip install` + `chat4000 pair`) — pair handles enabling
    # the plugin in config, minting the key, AND making sure the gateway
    # picks both up. Without this, users still need a 3rd command.
    _restart_hermes_gateway()

    click.echo(f"Pairing code: {code}")
    _render_qr_if_possible(f"chat4000://pair?code={code}")
    click.echo("Press Ctrl-C to stop pairing.")
    click.echo("Status: [1/5] Opening pairing session")

    def on_status(status: PairHostStatus, detail: str) -> None:
        prefix = {
            "connecting": "[1/5]",
            "connected": "[2/5]",
            "waiting": "[3/5]",
            "joiner-ready": "[4/5]",
            "grant-sent": "[4/5]",
            "completed": "[5/5]",
            "closed": "[x]",
        }.get(status, "[*]")
        click.echo(f"Status: {prefix} {detail}")

    try:
        result = await host_pairing_session(
            PairHostOptions(
                relay_url=acct.relay_url,
                group_key_bytes=group_key_bytes,
                code=code,
                log_level=pll,
                on_status=on_status,
            )
        )
    except Exception as exc:
        log_path = dump_chat4000_trace(
            "cli-pair", exc, {"account_id": acct.account_id, "code": code}
        )
        click.echo("")
        click.echo(f"Pairing ended: {exc}")
        click.echo(f"Trace log: {log_path}")
        click.echo(
            "If this happened after about 60 seconds, the relay path "
            "likely idled out the WebSocket."
        )
        click.echo('Try again with: "hermes chat4000 pair"')
        return False

    click.echo(f"Pairing room: {result.room_id}")
    final = resolve_chat4000_account(_load_hermes_config(), acct.account_id)
    click.echo(f"Connected group: {final.group_id or '(local key ready)'}")
    return True


async def _run_pair_many_command(
    *,
    account_id: str,
    code: str,
    max_pairings: Optional[int],
    delay_ms: int,
    pairing_log_level: Optional[str],
) -> None:
    import click  # type: ignore[import-not-found]
    from .pairing import ContinuousHostOptions

    code = _validate_user_supplied_code(code)
    _ensure_plugin_enabled_in_hermes_config()
    cfg = _load_hermes_config()
    acct = resolve_chat4000_account(cfg, account_id)
    pll = _normalize_log_level(pairing_log_level or acct.pairing_log_level)
    group_key_bytes = _ensure_local_key_for_account(acct)

    click.echo(f"Pairing code: {code}")
    _render_qr_if_possible(f"chat4000://pair?code={code}")
    click.echo(
        f"Continuous pairing — Ctrl+C to stop. Max: "
        f"{max_pairings if max_pairings else 'unlimited'}. "
        f"Delay: {delay_ms}ms."
    )

    current_iteration = 1

    def on_status(status: PairHostStatus, detail: str) -> None:
        prefix = {
            "connecting": "[1/5]", "connected": "[2/5]", "waiting": "[3/5]",
            "joiner-ready": "[4/5]", "grant-sent": "[4/5]",
            "completed": "[5/5]", "closed": "[x]",
        }.get(status, "[*]")
        click.echo(f"[#{current_iteration}] Status: {prefix} {detail}")

    def on_paired(seq: int, result) -> None:
        nonlocal current_iteration
        click.echo(f"Paired device #{seq} (room={result.room_id[:12]}...)")
        current_iteration = seq + 1

    def on_iteration_error(exc, seq) -> None:
        log_path = dump_chat4000_trace(
            "cli-pair-many", exc,
            {"account_id": acct.account_id, "code": code, "sequence": seq},
        )
        click.echo(f"[#{current_iteration}] iteration error: {exc}")
        click.echo(f"Trace log: {log_path}")

    started = asyncio.get_event_loop().time()
    total = await host_pairing_session_continuous(
        ContinuousHostOptions(
            relay_url=acct.relay_url,
            group_key_bytes=group_key_bytes,
            code=code,
            log_level=pll,
            max_pairings=max_pairings,
            iteration_delay_secs=max(0, delay_ms) / 1000.0,
            on_status=on_status,
            on_paired=on_paired,
            on_iteration_error=on_iteration_error,
        )
    )
    elapsed = max(1, int(asyncio.get_event_loop().time() - started))
    click.echo(
        f"Done. Paired {total} device{'' if total == 1 else 's'} in {elapsed}s."
    )


def _run_reset_command(account_id: str) -> None:
    """Wipe local state. Destructive and irreversible — paired remote
    devices keep their old group key and will silently fail to decrypt
    anything from the new identity until they re-pair."""
    import click  # type: ignore[import-not-found]

    aid = (account_id or "default").strip() or "default"
    removed: list[str] = []

    key_path = resolve_chat4000_key_file_path(aid)
    if key_path.exists():
        try:
            key_path.unlink()
            removed.append(str(key_path))
        except OSError:
            pass

    db_path = resolve_ack_store_path(aid)
    cleanup_stale_ack_store_lock(db_path)
    for p in (
        db_path,
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-shm"),
        Path(str(db_path) + "-journal"),
    ):
        if p.exists():
            try:
                p.unlink()
                removed.append(str(p))
            except OSError:
                pass

    if not removed:
        click.echo(f'No local chat4000 state for account "{aid}".')
        return
    click.echo(f'Reset chat4000 account "{aid}". Removed:')
    for p in removed:
        click.echo(f"  {p}")
    click.echo('Re-pair with: "hermes chat4000 setup"')


# ─── Helpers ──────────────────────────────────────────────────────────────


def _normalize_log_level(value: Optional[str]) -> str:
    return "debug" if (value or "").strip().lower() == "debug" else "info"


def _load_hermes_config() -> dict:
    """Best-effort load of Hermes' config.yaml. If the loader isn't
    importable (running outside the Hermes process), return empty."""
    try:
        from hermes_cli.config import cfg_get  # type: ignore[import-not-found]
        # cfg_get supports dotted paths; pull the chat4000 section.
        channel = cfg_get("channels.chat4000") or {}
        return {"channels": {"chat4000": channel}}
    except Exception:
        return {}


def _ensure_plugin_enabled_in_hermes_config() -> None:
    """Add 'chat4000' to Hermes' `plugins.enabled` list in config.yaml.

    Pip-installed plugins are discovered via the `hermes_agent.plugins`
    entry-point but only ACTIVATED when their name appears in
    `plugins.enabled`. `hermes plugins enable chat4000` only works for
    directory-installed plugins (Hermes CLI gap), so we write the config
    ourselves the first time the user runs a CLI command. Idempotent.

    Runs at every CLI entry-point so users who pip-install and reset
    config don't end up with a broken state — re-running `chat4000
    pair` repairs the config too."""
    try:
        import yaml  # type: ignore[import-not-found]
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
    except Exception:
        return
    try:
        cfg_path = get_hermes_home() / "config.yaml"
    except Exception:
        return
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
        if not isinstance(cfg, dict):
            cfg = {}
        plugins = cfg.setdefault("plugins", {})
        if not isinstance(plugins, dict):
            plugins = {}
            cfg["plugins"] = plugins
        enabled = plugins.setdefault("enabled", [])
        if not isinstance(enabled, list):
            enabled = []
            plugins["enabled"] = enabled
        if "chat4000" in enabled:
            return  # already enabled, no-op
        enabled.append("chat4000")
        cfg_path.write_text(yaml.safe_dump(cfg))
        import click  # type: ignore[import-not-found]
        click.echo(f"Enabled chat4000 in Hermes config: {cfg_path}")
    except Exception:
        # Auto-enable is best-effort — operator can always do it manually.
        pass


def _restart_hermes_gateway() -> None:
    """Bounce the Hermes gateway in the background.

    Why this lives in the plugin: a fresh `chat4000 pair` may have
    just enabled the plugin and minted the key. If the gateway was
    already running, it never saw the config / validate_config change
    (Hermes only discovers plugins at startup) and won't load chat4000
    until restart. Bouncing makes the 2-command install actually work:

        $ uv pip install ...
        $ chat4000 pair      ← this also (re)starts the gateway

    Best-effort. If we can't find `hermes` on PATH, no-op."""
    import shutil
    import subprocess

    hermes = shutil.which("hermes")
    if not hermes:
        return
    # Kill any existing gateway (SIGKILL — Hermes traps SIGTERM with a slow
    # shutdown that can race with the new gateway's start).
    try:
        subprocess.run(
            ["pkill", "-9", "-f", "hermes gateway run"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass
    # Give the OS a tick to reap the killed process.
    import time
    time.sleep(1)
    # Start a new gateway. nohup + start_new_session so the gateway
    # survives the shell exiting / Ctrl-Z / ssh disconnect.
    try:
        log_path = "/tmp/gateway.log"
        with open(log_path, "ab") as logf:
            subprocess.Popen(
                [hermes, "gateway", "run"],
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        import click  # type: ignore[import-not-found]
        click.echo(f"(Re)started Hermes gateway in background. Log: {log_path}")
    except Exception as exc:
        import click  # type: ignore[import-not-found]
        click.echo(f"Note: could not auto-start gateway: {exc}", err=True)


def _ensure_local_key_for_account(account) -> bytes:
    from .crypto import generate_group_key

    access = inspect_chat4000_state_access(account.account_id)
    if access.has_ownership_mismatch and not access.can_auto_repair_ownership:
        raise RuntimeError(
            f"chat4000 state dir is owned by uid {access.preferred_owner_uid}, "
            f"but this command runs as uid {access.current_uid}. "
            f"Run `hermes chat4000 pair` as the same user that runs Hermes. "
            f"State dir: {access.state_dir}"
        )

    group_key_bytes = bytes(account.group_key_bytes)
    if len(group_key_bytes) != 32:
        group_key_bytes = generate_group_key()
        stored = save_stored_group_key(account.account_id, group_key_bytes)
        import click  # type: ignore[import-not-found]
        click.echo(f"Created local chat4000 key.\nKey file: {stored.path}")
    return group_key_bytes


def _render_qr_if_possible(payload: str) -> None:
    """Render a QR code to the terminal when stdout is a TTY and
    `qrcode` is installed. Silently fall back to printing the payload."""
    import click  # type: ignore[import-not-found]

    click.echo(f"QR payload: {payload}")
    if not sys.stdout.isatty():
        return
    try:
        import qrcode  # type: ignore[import-not-found]

        qr = qrcode.QRCode(border=1)
        qr.add_data(payload)
        qr.make(fit=True)
        # Compact unicode rendering — half-block per cell.
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        click.echo(buf.getvalue())
    except Exception:
        # Not having `qrcode` installed is fine — we always print the
        # payload above so the user can paste it into a QR generator.
        pass


def _handle_cli_error(exc: BaseException) -> None:
    import click  # type: ignore[import-not-found]
    log_path = dump_chat4000_trace("cli", exc)
    click.echo(f"chat4000 error: {exc}", err=True)
    click.echo(f"Trace log: {log_path}", err=True)
