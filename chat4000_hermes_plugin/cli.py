"""CLI subcommands: `chat4000 pair|status|reset|uninstall|wizard|telemetry` (v2 / Matrix).

Pairing is now a 6-digit OTP via the registrar (protocol C), not the v1 relay
handshake:

  pair   — run setup (C.6: POST /plugins, PUT /user, space + control room +
           invites) on first run, then register a 6-digit user code BOUND to the
           plugin's one user, show it (+ QR), and poll for immediate feedback.
           The gateway-resident completion listener is the system of record for
           the code's whole lifetime; this watcher is install feedback only.
           `--ttl <seconds>` and `--reusable` pass through to register (C.1).
  prepare — setup only (identity + user + rooms), no pairing code.
  status — show the bot identity + paired users.
  reset  — wipe bot creds + crypto store + known users (destructive).
  uninstall — disable the plugin in the Hermes config + delete ALL plugin state
           (every account), then print the pip step (destructive, global).
  wizard — DEPRECATED (the installer owns interactive installs); prints a note,
           then still runs the legacy wizard so old installers keep working.
  telemetry — opt in/out (unchanged).

Config (env): CHAT4000_REGISTRAR_URL (default prod), CHAT4000_SERVICE_TOKEN
(required for register/status — see pushback X2).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from . import registrar_config
from .matrix.creds_store import crypto_store_path, load_bot_creds
from .matrix.registrar_client import RegistrarClient, RegistrarError
from .matrix.users_store import add_known_user, load_known_users
from .package_info import read_package_version
from .telemetry import (
    flush_chat4000_telemetry,
    get_telemetry_status,
    initialize_chat4000_telemetry,
    set_telemetry_enabled,
)

APP_ID = registrar_config.PLUGIN_APP_ID

# QR payload base for pairing. An https universal link (not the chat4000://
# scheme) so any stock camera app can scan it.
PAIR_LINK_BASE = "https://pair.chat4000.com"

REGISTRAR_URLS = registrar_config.REGISTRAR_URLS
DEFAULT_SERVICE_TOKEN = registrar_config.DEFAULT_SERVICE_TOKEN


def _env_file_path() -> Path:
    return registrar_config.env_file_path()


def _load_persisted_env() -> str:
    return registrar_config.load_persisted_env()


def _persist_env(env: str) -> None:
    registrar_config.persist_env(env)


def _resolve_env() -> str:
    return registrar_config.resolve_env()


def _resolve_registrar_url() -> str:
    return registrar_config.resolve_registrar_url()


def _registrar() -> RegistrarClient:
    return registrar_config.build_registrar_client()


def _gen_code() -> str:
    """A 6-digit CSPRNG OTP (always exactly 6 digits)."""
    return f"{secrets.randbelow(900000) + 100000:06d}"


def _build_chat4000_cli() -> Any:  # noqa: ANN401  # returns a click.Group (untyped click object)
    import click

    @click.group(name="chat4000", help="Manage chat4000 (Matrix) onboarding and pairing")
    @click.option(
        "--no-telemetry",
        is_flag=True,
        default=False,
        help="Disable anonymous error reporting for this run.",
    )
    @click.pass_context
    def chat4000(ctx_obj: Any, no_telemetry: bool) -> None:  # noqa: ANN401  # click pass_context object (untyped)
        if no_telemetry:
            os.environ["CHAT4000_TELEMETRY_DISABLED"] = "1"

    @chat4000.command("pair")
    @click.option("--account", default="default", help="Account id")
    @click.option(
        "--stage",
        is_flag=True,
        default=False,
        help="Pair against the stage servers (stgcht4.duckdns.org).",
    )
    @click.option(
        "--ttl",
        "ttl_seconds",
        type=int,
        default=None,
        help="Code lifetime in seconds (1-63072000, i.e. up to 2 years; "
        "default: server config). Long-lived codes are standing credentials — "
        "use the shortest TTL the use case allows.",
    )
    @click.option(
        "--reusable",
        is_flag=True,
        default=False,
        help="The code can be redeemed many times until expiry, each redeem "
        "adding another device (fleet enrollment; single-use is the default).",
    )
    @click.option(
        "--code",
        "custom_code",
        default=None,
        help="Use this exact code instead of a random one (must be exactly 6 "
        "digits). Rejected with 'code in use' if it is already active.",
    )
    def cmd_pair(
        account: str,
        stage: bool,
        ttl_seconds: int | None,
        reusable: bool,
        custom_code: str | None,
    ) -> None:
        """Set up (first run) and pair a device with a 6-digit code."""
        if custom_code is not None and not (custom_code.isdigit() and len(custom_code) == 6):
            raise click.BadParameter("code must be exactly 6 digits", param_hint="--code")
        if stage:
            os.environ["CHAT4000_ENV"] = "stage"
        # Persist whatever environment we're pairing against so future
        # invocations (status, a fresh shell, the gateway) stay on it.
        _persist_env(_resolve_env())
        try:
            asyncio.run(
                _run_pair(account, ttl_seconds=ttl_seconds, reusable=reusable, code=custom_code)
            )
        except KeyboardInterrupt:
            click.echo("\nPairing cancelled.")
        except Exception as exc:  # noqa: BLE001
            _handle_cli_error(exc)

    @chat4000.command("prepare")
    @click.option("--account", default="default", help="Account id")
    @click.option(
        "--stage",
        is_flag=True,
        default=False,
        help="Prepare against the stage servers (stgcht4.duckdns.org).",
    )
    def cmd_prepare(account: str, stage: bool) -> None:
        """Plugin setup (protocol C.6): persist the env, enable the plugin in the
        Hermes config, self-onboard the bot identity, ensure the plugin's one
        user (PUT /user), and create the space + control room + invites via a
        short-lived bot session — all BEFORE any device pairs. Idempotent.
        Fails fast if the registrar is unreachable."""
        if stage:
            os.environ["CHAT4000_ENV"] = "stage"
        _persist_env(_resolve_env())
        _ensure_plugin_enabled_in_hermes_config()
        try:
            asyncio.run(_run_prepare(account))
        except KeyboardInterrupt:
            click.echo("\nCancelled.")
        except Exception as exc:  # noqa: BLE001
            _handle_cli_error(exc)

    @chat4000.command("status")
    @click.option("--account", default="default", help="Account id")
    def cmd_status(account: str) -> None:
        """Show the bot identity and paired users."""
        import click as _c

        creds = load_bot_creds(account)
        users = load_known_users(account)
        if creds is None:
            _c.echo("configured: no  (run `chat4000 pair`)")
            return
        _c.echo(
            "\n".join(
                [
                    f"account:     {account}",
                    f"environment: {_resolve_env()}",
                    f"bot user:    {creds.user_id}",
                    f"device:      {creds.device_id}",
                    f"gateway:     {creds.gateway_url}",
                    f"paired users: {len(users)}" + (": " + ", ".join(users) if users else ""),
                    "configured:  yes",
                ]
            )
        )

    @chat4000.command("reset")
    @click.option("--account", default="default", help="Account id")
    def cmd_reset(account: str) -> None:
        """Wipe bot creds + crypto store + known users (destructive)."""
        _run_reset(account)

    @chat4000.command("uninstall")
    @click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
    def cmd_uninstall(yes: bool) -> None:
        """Fully remove chat4000: disable it in the Hermes config and delete ALL
        plugin state (every account's creds, crypto store, known/onboarded users,
        env, logs). Destructive + global. Prints the pip step to finish removal."""
        if not yes and not click.confirm(
            "Remove ALL chat4000 plugin state and disable it in the Hermes config?"
        ):
            click.echo("Uninstall cancelled.")
            return
        _run_uninstall()

    @chat4000.command("wizard")
    def cmd_wizard() -> None:
        """[DEPRECATED] Interactive install wizard — use the chat4000 installer."""
        # Retired as the driven flow: the installer owns interactive installs.
        # The wizard still RUNS after the note so existing installers that
        # hand off to `chat4000 wizard` keep working (do not delete code).
        click.echo("NOTE: `chat4000 wizard` is deprecated — the chat4000 installer now")
        click.echo("owns interactive installs. Prefer the one-liner:")
        click.echo("")
        click.echo(
            "  curl -fsSL https://raw.githubusercontent.com/chat4000/"
            "chat4000-installer/main/install.sh | bash"
        )
        click.echo("")
        from .install_wizard import main as wizard_main

        sys.exit(wizard_main())

    @chat4000.group("telemetry")
    def telemetry_group() -> None:
        """Manage anonymous error reporting."""

    @telemetry_group.command("status")
    def cmd_tel_status() -> None:
        import click as _c

        s = get_telemetry_status()
        _c.echo(f"Telemetry: {'enabled' if s['enabled'] else 'disabled'} ({s['reason']})")

    @telemetry_group.command("disable")
    def cmd_tel_disable() -> None:
        import click as _c

        from . import analytics

        # DEC3: no machine-side telemetry_preference_changed event — the toggle
        # just goes quiet (the iOS event of the same name is the client's own).
        analytics.shutdown()
        set_telemetry_enabled(False)
        _c.echo("Telemetry disabled.")

    @telemetry_group.command("enable")
    def cmd_tel_enable() -> None:
        import click as _c

        set_telemetry_enabled(True)
        _c.echo("Telemetry enabled.")

    return chat4000


def register_chat4000_cli(ctx: Any) -> None:  # noqa: ANN401  # Hermes host plugin context (untyped host object)
    ctx.register_cli(_build_chat4000_cli())


def main() -> None:
    import atexit
    import signal

    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    initialize_chat4000_telemetry()
    from . import analytics

    analytics.initialize_chat4000_analytics()

    def _flush_telemetry() -> None:
        # CLI commands are short-lived — drain BOTH PostHog and Sentry on ANY exit
        # path (incl. sys.exit) so every analytics event and crash report lands
        # before the process dies. Both flushes are best-effort no-ops when their
        # system never initialized.
        analytics.flush()
        flush_chat4000_telemetry()

    atexit.register(_flush_telemetry)

    # SIGINT/SIGTERM (Ctrl-C, container stop) bypass atexit unless we translate
    # them into a normal exit — do that AFTER flushing, so a terminated CLI run
    # (e.g. `pair` waiting on a device) still ships its queued events.
    def _on_signal(_signum: int, _frame: object) -> None:
        _flush_telemetry()
        sys.exit(0)

    for _sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(_sig, _on_signal)

    cli = _build_chat4000_cli()
    try:
        cli(prog_name="chat4000")
    except BrokenPipeError:
        sys.exit(0)


# ─── pair flow ──────────────────────────────────────────────────────────────


async def _run_pair(
    account: str,
    *,
    ttl_seconds: int | None = None,
    reusable: bool = False,
    code: str | None = None,
) -> None:
    import click

    from . import analytics

    version = read_package_version()
    reg = _registrar()
    env = _resolve_env()
    click.echo(f"Environment: {env}  (registrar: {_resolve_registrar_url()})")

    # Pip-installed Hermes plugins are DISCOVERED via entry-points but only
    # LOADED when listed in plugins.enabled. Without this, the gateway never
    # loads chat4000 → never connects → no rooms. (v1 did this; the v2 rewrite
    # dropped it — that's why room creation silently did nothing.)
    _ensure_plugin_enabled_in_hermes_config()

    # DEC3: no plugin-side pair-funnel events — the funnel is observed
    # registrar-side (pairing_started/completed/expired, version_checked).

    # Version gate (C.5) — refuse on force_upgrade, report to the operator.
    # The registrar's own version_checked row (RG1) records the verdict.
    try:
        verdict = await reg.version(APP_ID, version, env, client_id=analytics.machine_client_id())
        if verdict.action == "force_upgrade":
            click.echo(f"Update required (>= {verdict.recommended}). Aborting.", err=True)
            sys.exit(2)
        if verdict.action == "recommend_upgrade":
            click.echo(f"Note: a newer version is recommended ({verdict.recommended}).")
    except RegistrarError as exc:
        click.echo(f"(version check skipped: {exc})")

    # Plugin setup (C.6, idempotent): bot identity, the plugin's one user
    # (PUT /user), and the space + control room + invites — all before any
    # device pairs, so pairing below is purely a device operation.
    from . import setup_flow

    outcome = await setup_flow.ensure_setup(account, registrar=reg)
    if outcome is None:
        click.echo("Could not onboard the plugin identity (registrar unreachable?).", err=True)
        return

    # Mint a pairing code — bound implicitly to the plugin's one DERIVED user
    # (C.3.1); the bot token alone selects it (no kind, no plugin_id, no
    # user_id). `ensure_setup` bound the bot token onto `reg`.
    # A caller-supplied `--code` (already validated as 6 digits) is used verbatim;
    # otherwise mint a random one. The registrar still enforces format + rejects
    # a collision with 'code in use' (M_CODE_IN_USE).
    code = code or _gen_code()
    register_resp = await reg.create_code(
        code,
        ttl_seconds=ttl_seconds,
        reusable=reusable,
    )
    # Record the outstanding code durably: the gateway-resident completion
    # listener owns it for its whole lifetime (C.4); the watcher below is only
    # immediate install feedback.
    import time as _time

    from .matrix.pair_codes_store import PendingCode, add_pending_code

    add_pending_code(
        PendingCode(
            code=code,
            expires_at_ms=int(register_resp.get("expires_at") or 0),
            reusable=reusable,
            registered_at_ms=int(_time.time() * 1000),
        ),
        account,
    )
    click.echo("")
    click.echo(f"  Pairing code:  {code[:3]}-{code[3:]}")
    # Universal link, not a custom scheme — any camera app can scan it: the
    # browser opens pair.chat4000.com, which deep-links into the app (or shows
    # install instructions when the app isn't there yet).
    _render_qr_if_possible(f"{PAIR_LINK_BASE}/?code={code}")
    click.echo("Enter this code in the chat4000 app. Waiting for the device…")
    click.echo("(Ctrl-C to stop.)")

    status = await reg.poll_until_complete(code)
    user_id = status.get("user_id") if status else None
    if not user_id:
        if reusable:
            # A reusable code never settles to `completed` (C.3) and may well be
            # long-lived: no redeem inside the watch window is NOT an expiry.
            # The resident listener keeps watching until the code's real TTL.
            # (No pairing_expired event either way — that's the registrar's row.)
            click.echo(
                "No device has redeemed the code yet. It stays active until it "
                "expires; the running gateway completes pairings as devices join."
            )
            return
        click.echo("Pairing code expired without a device redeeming it. Try again.")
        # IN10: a real expiry must signal FAILURE via a non-zero exit. The
        # installer reads ONLY our exit code to judge pairing; a 0 here made it
        # report a false "device paired" success on an expired code.
        raise SystemExit(1)

    add_known_user(user_id, account)
    # PL4/FLW3-4: pairing_completed once per redeemed device, deduped against
    # the gateway-resident listener via the pending-codes store.
    _track_watcher_redeems(status or {}, code=code, account=account, reusable=reusable)
    analytics.flush()
    click.echo("")
    click.echo(f"✓ Paired {user_id}.")
    click.echo("A running gateway invites them + shares keys within a few seconds (no restart).")
    if reusable:
        click.echo(
            "The code is reusable — it stays active until expiry, and the running "
            "gateway enrolls further devices that redeem it automatically."
        )


def _track_watcher_redeems(
    status: dict[str, Any], *, code: str, account: str, reusable: bool
) -> None:
    """PL4: emit `pairing_completed` once per redeem the CLI watcher observed,
    deduped per device against the gateway-resident listener through the
    pending-codes store's `redeemed_count_seen` check-and-set — the SAME field
    the listener advances, so whichever poller records a redeem first reports
    it and the other skips (the listener processes only entries beyond the
    recorded count)."""
    from . import analytics
    from .matrix.pair_codes_store import load_pending_codes, update_pending_code
    from .matrix.registrar_client import pair_redeem_index

    redeems = [e for e in (status.get("redeems") or []) if isinstance(e, dict)]
    count = int(status.get("redeemed_count") or 0) or len(redeems)
    if not redeems:
        # Old-registrar completed shape (no redeems[]): synthesize the one
        # redeem from the top-level fields so the completion still counts.
        redeems = [{"device_id": None, "client_id": status.get("client_id")}]
        count = count or 1
    record = next((r for r in load_pending_codes(account) if r.code == code), None)
    seen = record.redeemed_count_seen if record is not None else 0
    if count <= seen:
        return  # the resident listener already recorded (and reported) these
    new_n = count - seen
    fresh = redeems[-new_n:] if new_n <= len(redeems) else redeems
    if record is not None:
        record.redeemed_count_seen = count
        update_pending_code(record, account)
    for entry in fresh:
        analytics.track_pairing_completed(
            str(entry.get("client_id") or "").strip() or None,
            reusable=reusable,
            redeem_index=pair_redeem_index(status, entry.get("device_id")),
        )


async def _run_prepare(account: str) -> None:
    """Plugin setup (C.6): bot identity, the plugin's one user, and the space +
    control room + invites — all created (idempotently) before any pairing."""
    import click

    from . import setup_flow

    outcome = await setup_flow.ensure_setup(account, registrar=_registrar())
    if outcome is None:
        click.echo("Onboard failed — is the registrar reachable?", err=True)
        sys.exit(1)
    click.echo(f"Plugin identity ready: {outcome.creds.user_id}")
    click.echo(f"Gateway: {outcome.creds.gateway_url}")
    click.echo(f"User: {outcome.user_id} ({'created' if outcome.user_created else 'existing'})")
    click.echo(f"Space: {outcome.space_id or '(missing)'}")
    click.echo(f"Control room: {outcome.control_room_id or '(missing)'}")


def _run_reset(account: str) -> None:
    import click

    from .key_store import resolve_chat4000_plugin_dir

    plugin_dir = resolve_chat4000_plugin_dir()
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (account or "default"))
    targets = [
        plugin_dir / f"matrix-creds-{safe}.json",
        plugin_dir / f"known-users-{safe}.json",
        plugin_dir / f"onboarded-{safe}.json",
        plugin_dir / f"pending-codes-{safe}.json",
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
    for removed_path in removed:
        click.echo(f"  {removed_path}")
    click.echo("Re-pair with: chat4000 pair")


def _run_uninstall() -> None:
    """Reverse the install footprint: disable the plugin in the Hermes config and
    delete the ENTIRE plugin state dir (all accounts). The pip package can't
    cleanly uninstall itself from inside its own running process, so we print that
    final step rather than attempt it."""
    import shutil

    import click

    from .key_store import resolve_chat4000_plugin_dir

    _disable_plugin_in_hermes_config()
    plugin_dir = resolve_chat4000_plugin_dir()
    existed = plugin_dir.exists()
    removed = False
    if existed:
        try:
            shutil.rmtree(plugin_dir)
            removed = True
        except OSError as exc:
            click.echo(f"Could not remove {plugin_dir}: {exc}", err=True)
    if removed:
        click.echo(f"Removed chat4000 plugin state: {plugin_dir}")
    else:
        click.echo("No chat4000 plugin state to remove.")
    pkg = _distribution_name()
    click.echo("")
    click.echo("To finish removing the plugin, uninstall the package and restart Hermes:")
    click.echo(f"  pip uninstall -y {pkg}")
    click.echo("  (then restart the Hermes gateway so it stops loading chat4000)")


def _distribution_name() -> str:
    """The installed pip distribution name, for the final `pip uninstall` hint.
    Falls back to the known package name if metadata lookup isn't available."""
    import contextlib

    with contextlib.suppress(Exception):
        from importlib.metadata import distributions

        for dist in distributions():
            top_level = dist.read_text("top_level.txt") or ""
            if "chat4000_hermes_plugin" in top_level.split():
                return str(dist.metadata["Name"])
    return "chat4000-hermes-plugin"


# ─── helpers ─────────────────────────────────────────────────────────────────


def _hermes_config_path() -> Path:
    """Resolve Hermes' config.yaml: prefer hermes_constants' home, else
    HERMES_HOME, else ~/.hermes."""
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

        return Path(get_hermes_home()) / "config.yaml"
    except ImportError:
        home = os.environ.get("HERMES_HOME", "").strip()
        return (Path(home).expanduser() if home else Path.home() / ".hermes") / "config.yaml"


def _disable_plugin_in_hermes_config() -> None:
    """Remove 'chat4000' from Hermes' plugins.enabled in config.yaml (idempotent).

    The inverse of `_ensure_plugin_enabled_in_hermes_config` — so after an
    uninstall the gateway stops loading the plugin on its next restart.
    Best-effort: no-op if yaml/config aren't available (operator edits manually)."""
    try:
        import yaml
    except ImportError:
        return
    cfg_path = _hermes_config_path()
    if not cfg_path.exists():
        return
    try:
        import click

        cfg = yaml.safe_load(cfg_path.read_text())
        if not isinstance(cfg, dict):
            return
        plugins = cfg.get("plugins")
        if not isinstance(plugins, dict):
            return
        enabled = plugins.get("enabled")
        if not isinstance(enabled, list) or "chat4000" not in enabled:
            return
        plugins["enabled"] = [p for p in enabled if p != "chat4000"]
        cfg_path.write_text(yaml.safe_dump(cfg))
        click.echo(f"Disabled chat4000 in Hermes config: {cfg_path}")
    except Exception as exc:  # noqa: BLE001
        # Best-effort config edit (operator can disable manually). Report once,
        # then continue — never let a config-write failure abort the uninstall.
        from .error_log import dump_chat4000_trace

        dump_chat4000_trace("cli.disable_plugin_config", exc)


def _ensure_plugin_enabled_in_hermes_config() -> None:
    """Add 'chat4000' to Hermes' plugins.enabled in config.yaml (idempotent).

    Pip-installed plugins are discovered via the hermes_agent.plugins entry-point
    but only activated when enabled here. Best-effort: if yaml/hermes_constants
    aren't importable, no-op (operator can enable manually)."""
    try:
        import yaml
    except ImportError:
        return
    cfg_path = _hermes_config_path()
    try:
        import click

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
            return
        enabled.append("chat4000")
        cfg_path.write_text(yaml.safe_dump(cfg))
        click.echo(f"Enabled chat4000 in Hermes config: {cfg_path}")
    except Exception as exc:  # noqa: BLE001
        # Best-effort config edit (operator can enable manually). Report once,
        # then continue — never block pairing on a config-write failure.
        from .error_log import dump_chat4000_trace

        dump_chat4000_trace("cli.enable_plugin_config", exc)


def _render_qr_if_possible(payload: str) -> None:
    import click

    click.echo(f"QR payload: {payload}")
    # Draw the scannable QR on a TERMINAL. Normally that's stdout — but the installer
    # runs us with stdout tee'd to its always-on /tmp debug log (a pipe, so
    # sys.stdout.isatty() is False) while the user is still sitting at a real
    # terminal. So when stdout isn't a tty, fall back to the controlling terminal
    # /dev/tty. If neither is a terminal (a truly headless / detached upgrade run),
    # skip — there is no human to scan it and the printed code + payload above are
    # enough. This lets the always-on debug log (the tee) AND the interactive QR
    # coexist: the QR targets the real terminal, not the tee'd stdout.
    term = None
    opened = False
    if sys.stdout.isatty():
        term = sys.stdout
    else:
        try:
            term = open("/dev/tty", "w")  # noqa: SIM115 — closed in finally
            opened = True
        except OSError:
            return  # no terminal anywhere — nothing to draw the QR on.
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(payload)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        term.write(buf.getvalue())
        term.flush()
    except ImportError:
        pass  # qrcode not installed — the textual code above is enough
    except Exception as exc:  # noqa: BLE001
        # QR rendering is a cosmetic extra; report once, the printed code stands.
        from .error_log import dump_chat4000_trace

        dump_chat4000_trace("cli.render_qr", exc)
    finally:
        if opened:
            try:
                term.close()
            except OSError:
                pass


def _handle_cli_error(exc: BaseException) -> None:
    """Report a CLI failure and exit NON-ZERO so callers know it failed.
    Operational registrar errors (backend down, bad token, code in use) are
    printed cleanly — NOT captured as Sentry crashes (and per DEC3 not tracked
    to PostHog either; the registrar observes its own failures); only
    unexpected errors get a Sentry trace."""
    import click

    if isinstance(exc, RegistrarError):
        click.echo(f"Pairing failed: {exc}", err=True)
        if exc.status in (0, 502, 503, 504):
            click.echo(
                "The registrar is down or unreachable — check the backend "
                "(or stage deploy) and try again.",
                err=True,
            )
        sys.exit(1)

    from .error_log import dump_chat4000_trace

    log_path = dump_chat4000_trace("cli", exc)
    click.echo(f"chat4000 error: {exc}", err=True)
    click.echo(f"Trace log: {log_path}", err=True)
    sys.exit(1)
