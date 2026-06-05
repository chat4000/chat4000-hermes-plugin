"""Interactive install wizard — `chat4000 wizard`.

Pretty TUI wrapper around the pair + gateway-restart flow. Designed to
be invoked by `install.sh` as the final hand-off, but also runnable
directly: `chat4000 wizard`.

Steps:
  1. Banner + Hermes-environment summary.
  2. Run the pair handshake (delegates to `chat4000 pair`).
  3. Detect whether the Hermes gateway is supervised (systemd, docker
     restart policy). After killing it, wait ~2 s — if a supervisor
     brings it back we skip manual restart; otherwise we start it
     ourselves via nohup.
  4. Tail the fresh gateway log briefly so the user sees boot output.
  5. Success panel with follow-up commands.

Pure stdlib + `rich`. No external orchestration; the wizard process
exits when the gateway is running and the pair is complete.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

console = Console()

# Icons — Unicode glyphs that render in any modern terminal.
ICO_OK = "✓"
ICO_ERR = "✗"
ICO_INFO = "ℹ"
ICO_WAIT = "⏳"
ICO_ROCKET = "🚀"
ICO_LOCK = "🔐"
ICO_PHONE = "📱"
ICO_WAVE = "👋"
ICO_SPARK = "✨"


def banner() -> None:
    console.print()
    console.print(
        Panel.fit(
            Text.assemble(
                (f"{ICO_LOCK}  ", "bold magenta"),
                ("chat4000", "bold magenta"),
                ("  ·  ", "dim"),
                ("Hermes plugin installer", "bold"),
                ("\n", ""),
                ("Native iPhone / Mac / CLI app for your Hermes agent", "dim"),
            ),
            border_style="magenta",
            padding=(0, 2),
        )
    )
    console.print()


def env_summary() -> dict[str, str]:
    """Detect Hermes paths + plugin version; print a small table."""
    hermes_cmd = shutil.which("hermes") or ""
    venv_bin = ""
    if hermes_cmd:
        import re

        m = re.search(r"/[^\"'\s]+/venv/bin", Path(hermes_cmd).read_text(errors="ignore"))
        venv_bin = m.group(0) if m else ""

    from .package_info import read_package_version

    plugin_version = read_package_version()

    # v2 identity = the Matrix bot creds (minted at pair time).
    from .matrix.creds_store import load_bot_creds

    paired = load_bot_creds("default") is not None

    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(style="dim")
    tbl.add_column()
    tbl.add_row("hermes", f"[cyan]{hermes_cmd or '(not on PATH)'}[/cyan]")
    tbl.add_row("venv", f"[cyan]{venv_bin or '(unknown)'}[/cyan]")
    tbl.add_row("plugin", f"[green]{plugin_version}[/green]")
    tbl.add_row(
        "paired",
        "[green]yes[/green]" if paired else "[yellow]no (will pair now)[/yellow]",
    )
    console.print(tbl)
    console.print()

    return {
        "hermes_cmd": hermes_cmd,
        "venv_bin": venv_bin,
        "plugin_version": plugin_version,
        "paired": "1" if paired else "",
    }


def rule(title: str, step: int, total: int) -> None:
    console.rule(
        Text.assemble(
            (f" Step {step}/{total} ", "on magenta white"),
            ("  ", ""),
            (title, "bold"),
        ),
        style="magenta",
        align="left",
    )


def _chat4000_bin(venv_bin: str) -> str:
    return (
        f"{venv_bin}/chat4000" if venv_bin and Path(f"{venv_bin}/chat4000").exists() else "chat4000"
    )


def _stage_args() -> list[str]:
    """Pass --stage through to chat4000 subcommands when the install is staged."""
    return ["--stage"] if os.environ.get("CHAT4000_ENV", "").strip().lower() == "stage" else []


def step_prepare(venv_bin: str) -> int:
    """Step 1: enable the plugin + persist env + self-onboard the bot identity, so
    the gateway can connect on the NEXT restart (before any pairing)."""
    rule(f"{ICO_LOCK}  Prepare the plugin", 1, 3)
    console.print("[dim]Registering the bot identity and enabling the plugin…[/dim]")
    args = [_chat4000_bin(venv_bin), "prepare", *_stage_args()]
    try:
        rc = subprocess.call(args)  # noqa: S603  # trusted fixed argv (resolved chat4000 path)
    except KeyboardInterrupt:
        return 130
    if rc != 0:
        console.print(f"[red]{ICO_ERR}  Prepare failed (exit {rc}).[/red]")
    return rc


def step_pair(venv_bin: str) -> int:
    """Step 3: run the pair handshake. Returns process exit code."""
    rule(f"{ICO_PHONE}  Pair a device", 3, 3)
    console.print(
        "[dim]Scan the QR with the chat4000 iOS/macOS app, "
        "or paste the code into the CLI client.[/dim]"
    )
    console.print("[dim]Press Ctrl-C any time to cancel.[/dim]")
    console.print()

    try:
        rc = subprocess.call(  # noqa: S603  # trusted fixed argv (resolved chat4000 path)
            [_chat4000_bin(venv_bin), "pair", *_stage_args()]
        )
    except KeyboardInterrupt:
        console.print(f"\n[yellow]{ICO_WAIT}  Pairing cancelled.[/yellow]")
        return 130
    if rc != 0:
        console.print(f"[red]{ICO_ERR}  Pairing failed (exit {rc}).[/red]")
        return rc
    console.print()
    console.print(f"[green]{ICO_OK}  Pair complete.[/green]")
    return 0


def gw_is_running() -> bool:
    """Detect a live `hermes gateway run` process."""
    try:
        subprocess.run(
            ["pgrep", "-f", "hermes gateway run"],  # noqa: S607  # trusted fixed argv
            check=True,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def wait_for_supervisor_restart(seconds: float = 2.0) -> bool:
    """After killing the gateway, poll briefly to see if a supervisor
    (systemd, docker restart policy, launchd) brings it back. Returns
    True if it came back on its own within the window."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if gw_is_running():
            return True
        time.sleep(0.15)
    return False


# Optional grace for Telegram to release the old gateway's getUpdates long-poll
# before starting the new one. DEFAULT 0 — we don't make users wait. The brief
# "polling conflict" Telegram logs during the overlap is transient (self-heals in
# a few seconds) and is SUPPRESSED from the display + gateway log instead (see
# logging_setup.suppress_telegram_polling_conflict). Set
# CHAT4000_TELEGRAM_RELEASE_SECS=6 to wait instead of suppress.
TELEGRAM_RELEASE_SECS = float(os.environ.get("CHAT4000_TELEGRAM_RELEASE_SECS", "0") or 0)


def _wait_until_gateway_gone(timeout: float = 10.0) -> None:
    """Poll until no `hermes gateway run` process remains (re-killing stragglers),
    up to `timeout`. Guarantees the old gateway can't overlap the new one."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not gw_is_running():
            return
        subprocess.run(
            ["pkill", "-9", "-f", "hermes gateway run"],  # noqa: S607  # trusted fixed argv
            capture_output=True,
        )
        time.sleep(0.3)


def _clear_ready_marker() -> None:
    """Delete the adapter's 'ready' marker so we only ever see a FRESH one written
    by the gateway we're about to start (not a stale one from a previous run)."""
    import contextlib

    from .key_store import resolve_chat4000_ready_marker

    with contextlib.suppress(OSError):
        resolve_chat4000_ready_marker().unlink(missing_ok=True)


def _ready_marker_fresh(min_mtime: float) -> bool:
    """True only if the 'ready' marker exists AND was written at/after `min_mtime`
    (i.e. by the gateway we just restarted, not a stale one from a prior run)."""
    from .key_store import resolve_chat4000_ready_marker

    try:
        return resolve_chat4000_ready_marker().stat().st_mtime >= min_mtime - 1.0
    except OSError:
        return False


def wait_for_gateway_ready(min_mtime: float, timeout: float = 180.0, est_secs: float = 90.0) -> int:
    """Block (with a progress bar) until the gateway has LOADED the chat4000 plugin
    and it reports fully connected (auth + rooms + first sync) — the adapter writes
    the 'ready' marker then. Only a marker written at/after `min_mtime` counts, so a
    stale marker from a prior run can't make the bar flash. On timeout, warn and
    continue (pairing can still proceed; the gateway may yet come up + live-invite)."""
    console.print(
        f"[dim]{ICO_WAIT}  Waiting for the gateway to load the chat4000 plugin and "
        "connect — this can take a minute or two…[/dim]"
    )
    start = time.time()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Loading the chat4000 plugin", total=est_secs)
        while True:
            elapsed = time.time() - start
            if _ready_marker_fresh(min_mtime):
                progress.update(task, completed=est_secs)
                console.print(f"[green]{ICO_OK}  Gateway is up — plugin connected.[/green]")
                return 0
            if elapsed >= timeout:
                progress.update(task, completed=est_secs)
                console.print(
                    f"[yellow]{ICO_INFO}  Gateway didn't report ready within "
                    f"{int(timeout)}s — continuing. "
                    "Check [cyan]tail -f /tmp/gateway.log[/cyan].[/yellow]"
                )
                return 0
            progress.update(task, completed=min(elapsed, est_secs - 1))
            time.sleep(1.0)


def step_gateway() -> int:
    """Step 2: (re)start the Hermes gateway and wait until it's fully connected."""
    rule(f"{ICO_ROCKET}  Bring the gateway online", 2, 3)
    # Anything written before this restart is stale; only a 'ready' marker with
    # mtime >= restart_t0 counts as THIS gateway loading the plugin.
    restart_t0 = time.time()
    _clear_ready_marker()

    was_running = gw_is_running()
    if was_running:
        console.print(f"[cyan]{ICO_INFO}  Gateway is running — killing to load new plugin.[/cyan]")
        subprocess.run(
            ["pkill", "-9", "-f", "hermes gateway run"],  # noqa: S607  # trusted fixed argv
            capture_output=True,
        )

        console.print(f"[cyan]{ICO_WAIT}  Waiting 2 s to see if a supervisor restarts it…[/cyan]")
        if wait_for_supervisor_restart(seconds=2.0):
            console.print(
                f"[green]{ICO_OK}  Gateway came back on its own (supervisor managed).[/green]"
            )
            return wait_for_gateway_ready(restart_t0)
        console.print(f"[yellow]{ICO_INFO}  No supervisor detected — starting manually.[/yellow]")
        # Clean restart: make sure the old gateway is fully gone, then let
        # Telegram release its getUpdates poll, so the new gateway never
        # double-polls the bot (no "Telegram polling conflict").
        _wait_until_gateway_gone(timeout=10.0)
        if TELEGRAM_RELEASE_SECS > 0:
            console.print(
                f"[dim]{ICO_WAIT}  Letting Telegram release the old poll "
                f"({TELEGRAM_RELEASE_SECS:.0f}s) to avoid a polling conflict…[/dim]"
            )
            time.sleep(TELEGRAM_RELEASE_SECS)
    else:
        console.print(f"[cyan]{ICO_INFO}  Gateway not currently running.[/cyan]")

    rc = start_gateway_nohup()
    if rc != 0:
        return rc
    return wait_for_gateway_ready(restart_t0)


def start_gateway_nohup() -> int:
    """Start the gateway in the background with nohup + new session."""
    hermes = shutil.which("hermes")
    if not hermes:
        console.print(f"[red]{ICO_ERR}  `hermes` not on PATH — can't start gateway.[/red]")
        return 1
    log_path = "/tmp/gateway.log"  # noqa: S108  # intentional well-known log path the user tails
    try:
        with open(log_path, "ab") as logf:
            proc = subprocess.Popen(  # noqa: S603  # trusted fixed argv (resolved hermes path)
                [hermes, "gateway", "run"],
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        console.print(f"[red]{ICO_ERR}  Could not start gateway: {exc}[/red]")
        return 1

    # Don't wait on the process; it runs in the background.
    console.print(
        f"[green]{ICO_OK}  Gateway started (pid {proc.pid}). Log: [cyan]{log_path}[/cyan][/green]"
    )
    time.sleep(2)  # let it write the first few lines
    tail_log_panel(log_path)
    return 0


def tail_log_panel(
    log_path: str = "/tmp/gateway.log",  # noqa: S108  # intentional well-known log path
    n: int = 12,
) -> None:
    p = Path(log_path)
    if not p.exists():
        return
    try:
        all_lines = p.read_text(errors="replace").splitlines()
        # Hide the transient Telegram restart noise — keep every other line.
        lines = [ln for ln in all_lines if "polling conflict" not in ln][-n:]
    except OSError:
        return
    if not lines:
        return
    console.print()
    console.print(
        Panel(
            "\n".join(lines),
            title=f"[dim]{log_path}[/dim]",
            border_style="dim",
            padding=(0, 1),
        )
    )


def _resolve_chat4000_cmd(venv_bin: str = "") -> str:
    """The command to show for running `chat4000`. If it's on PATH (e.g. the
    installer symlinked it into /usr/local/bin), show bare `chat4000`. Otherwise
    fall back to the full venv-bin path so the command still works."""
    if shutil.which("chat4000"):
        return "chat4000"
    if venv_bin and Path(f"{venv_bin}/chat4000").exists():
        return f"{venv_bin}/chat4000"
    return "chat4000"


def success_panel(chat4000_cmd: str = "chat4000") -> None:
    console.print()
    console.print(
        Panel.fit(
            Text.assemble(
                (f"{ICO_SPARK}  ", "bold green"),
                ("Setup complete!", "bold green"),
                ("\n\n", ""),
                ("Send a message from the chat4000 app — your Hermes agent will reply.", ""),
                ("\n\n", ""),
                ("Useful commands ", "bold"),
                ("(it's a standalone command, not `hermes chat4000`):\n", "dim"),
                (f"  {chat4000_cmd} status", "cyan"),
                ("    show config + paired users\n", "dim"),
                (f"  {chat4000_cmd} pair", "cyan"),
                ("      pair another device\n", "dim"),
                ("  tail -f /tmp/gateway.log", "cyan"),
                ("    follow gateway logs", "dim"),
            ),
            border_style="green",
            padding=(0, 2),
        )
    )
    console.print()


def main() -> int:
    """Entry point — `chat4000 wizard`."""
    banner()
    env = env_summary()

    if not env["hermes_cmd"]:
        console.print(
            f"[red]{ICO_ERR}  `hermes` not found on PATH. "
            f"Install Hermes Agent first, then re-run the wizard.[/red]"
        )
        return 1

    venv_bin = env["venv_bin"]

    # Gateway-first: prepare identity → bring the gateway fully up (with the
    # loading bar) → THEN pair. The running gateway live-invites the paired user.
    rc = step_prepare(venv_bin)
    if rc != 0:
        return rc

    rc = step_gateway()
    if rc != 0:
        return rc

    rc = step_pair(venv_bin)
    if rc != 0:
        return rc

    success_panel(_resolve_chat4000_cmd(venv_bin))
    return 0


if __name__ == "__main__":
    sys.exit(main())
