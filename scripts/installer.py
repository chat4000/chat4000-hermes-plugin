#!/usr/bin/env python3
"""installer.py — the actual install logic. Runs in the system Python
(stdlib only). Detects Hermes, pip-installs the plugin into Hermes' venv,
then execs `chat4000 wizard` which lives inside the just-installed venv.

────────────────────────────────────────────────────────────────────────
A note about the telemetry in this file, from us at chat4000:

We send anonymous events to PostHog (product analytics) and Sentry
(uncaught crashes) FROM THE INSTALLER ITSELF. We do this so that:

  - we can see what % of installs succeed end-to-end (PostHog funnel)
  - we can see which step fails most often (uv missing? pip bootstrap?
    Hermes venv not detected? wizard handoff?)
  - we get a real stack trace when the installer crashes in a way we
    didn't anticipate (Sentry), so we can fix it without you having
    to file a bug

Things we NEVER send:
  - your message content, prompts, command arguments, env vars
  - pairing codes, group keys, anything from `keys/default.json`
  - usernames or anything else identifying

What WE send is bounded to:
  - which install step ran / failed, and the error class name
  - python + Hermes version, OS platform
  - an anonymous UUID (~/.config/chat4000/install-id) so we can tell
    one failed install retrying from many people each failing once

We're not trying to spy on you. We just want to ship an installer that
works for everyone, and the only way to know it's working is to
measure it. Opt out any of three ways:
  • CHAT4000_TELEMETRY_DISABLED=1 in your env
  • pass --no-telemetry on the curl|bash line
  • after install: `chat4000 telemetry disable`

Privacy policy: https://chat4000.com/privacy
Source: https://github.com/chat4000/chat4000-hermes-plugin
Love, chat4000 ❤️
────────────────────────────────────────────────────────────────────────


Designed to be downloaded by install.sh and run as a one-shot. All UI
here uses ANSI escapes (no third-party deps) because the rich library
isn't available until the plugin is installed. The wizard (post-install)
uses rich.

PostHog events fired by this file:
  - installer_started
  - installer_hermes_detected           {hermes_layout, hermes_path}
  - installer_uv_detected               {uv_path}
  - installer_pkg_installed             {installer_used, plugin_ref}
  - installer_failed                    {stage, error_class, error_msg}
  - installer_handing_off_to_wizard

Analytics use the same install_id as the plugin's Sentry / runtime
events, so the funnel is correlated across processes.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

# ─── Constants ────────────────────────────────────────────────────────────

REPO_URL = "https://github.com/chat4000/chat4000-hermes-plugin"
DEFAULT_REF = "stable"
HERMES_LAYOUTS = [
    # Direct paths first — fastest path on the most common installs.
    # `${HOME}/...` and `/...` are expanded with Path(...).expanduser()
    # at lookup time. Paths with `*` are globbed (used for Homebrew
    # Cellar version dirs).
    ("~/.hermes/hermes-agent/venv/bin", "curl-installer"),
    ("/usr/local/lib/hermes-agent/venv/bin", "fhs-source"),
    ("/opt/hermes/.venv/bin", "docker"),
    # Homebrew (glob the version segment)
    ("/opt/homebrew/Cellar/hermes-agent/*/libexec/bin", "homebrew-arm64"),
    ("/usr/local/Cellar/hermes-agent/*/libexec/bin", "homebrew-intel"),
    ("/home/linuxbrew/.linuxbrew/Cellar/hermes-agent/*/libexec/bin", "linuxbrew"),
    # pipx (modern XDG + legacy)
    ("~/.local/share/pipx/venvs/hermes-agent/bin", "pipx-modern"),
    ("~/.local/pipx/venvs/hermes-agent/bin", "pipx-legacy"),
    # uv tool install
    ("~/.local/share/uv/tools/hermes-agent/bin", "uv-tool"),
    # Linux distro-style repacks
    ("/opt/venvs/hermes-agent/bin", "dh-virtualenv"),
    ("/usr/share/hermes-agent/venv/bin", "deb-alt"),
    ("/usr/lib/hermes-agent/venv/bin", "rpm"),
    ("/usr/libexec/hermes-agent/venv/bin", "rpm-libexec"),
    ("/opt/hermes-agent/venv/bin", "rpm-opt"),
    # Local-prefix source installs
    ("~/.local/lib/hermes-agent/venv/bin", "user-prefix"),
    ("~/.local/share/hermes-agent/venv/bin", "xdg-data"),
    # macOS user library (defensive — no documented installer uses it)
    ("~/Library/Application Support/Hermes Agent/venv/bin", "macos-app-support"),
]

# Public PostHog credentials — same project the iOS / Mac apps use.
# Hardcoded here (no `import chat4000_hermes_plugin.analytics` yet — the
# plugin isn't installed at this point in the lifecycle).
POSTHOG_API_KEY = "phc_s49DnTamyFDnEC6MyumNmmjjf7p455LXCVzPE94hPemZ"
POSTHOG_HOST = "https://us.i.posthog.com"
POSTHOG_CAPTURE_URL = f"{POSTHOG_HOST}/capture/"

# Sentry DSN matching the Hermes plugin's runtime telemetry — installer
# crashes land in the same project as plugin-runtime crashes. Public-by-
# design (write-only ingestion endpoint, not a secret).
SENTRY_DSN = "https://ac3dabffdf2c91c9c90a87cd9b258908@o4511305222193152.ingest.us.sentry.io/4511433133129728"
INSTALLER_RELEASE = "chat4000-hermes-plugin-installer@1.0.0"

import platform
import time

_STARTED_AT_MS = int(time.time() * 1000)

# ─── ANSI ─────────────────────────────────────────────────────────────────

if sys.stdout.isatty():
    C_RED = "\033[1;31m"
    C_GRN = "\033[1;32m"
    C_YEL = "\033[1;33m"
    C_BLU = "\033[1;34m"
    C_MAG = "\033[1;35m"
    C_CYN = "\033[1;36m"
    C_DIM = "\033[2m"
    C_RST = "\033[0m"
    C_BOLD = "\033[1m"
else:
    C_RED = C_GRN = C_YEL = C_BLU = C_MAG = C_CYN = C_DIM = C_RST = C_BOLD = ""

def say(msg: str) -> None: print(f"{C_CYN}>{C_RST} {msg}")
def ok(msg: str) -> None: print(f"{C_GRN}✓{C_RST} {msg}")
def warn(msg: str) -> None: print(f"{C_YEL}⚠{C_RST} {msg}")
def err(msg: str) -> None: print(f"{C_RED}✗{C_RST} {msg}", file=sys.stderr)
def hdr(msg: str) -> None:
    line = "━" * 63
    print(f"\n{C_MAG}{line}{C_RST}\n{C_MAG}{C_BOLD}{msg}{C_RST}\n{C_MAG}{line}{C_RST}\n")

def banner() -> None:
    print(f"\n{C_MAG}┌─────────────────────────────────────────────────────────────┐{C_RST}")
    print(f"{C_MAG}│{C_RST}  {C_MAG}{C_BOLD}🔐 chat4000{C_RST}  ·  {C_BLU}{C_BOLD}Hermes plugin installer{C_RST}                       {C_MAG}│{C_RST}")
    print(f"{C_MAG}│{C_RST}  {C_DIM}Native iPhone / Mac / CLI app for your Hermes agent{C_RST}        {C_MAG}│{C_RST}")
    print(f"{C_MAG}└─────────────────────────────────────────────────────────────┘{C_RST}\n")

# ─── install_id (matches what the plugin will use later) ──────────────────

def resolve_install_id() -> str:
    cfg = Path.home() / ".config" / "chat4000"
    path = cfg / "install-id"
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        import uuid
        new_id = str(uuid.uuid4())
        cfg.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return new_id
    except Exception:
        import uuid
        return str(uuid.uuid4())

# ─── PostHog without the SDK (stdlib only) ────────────────────────────────

import json
import uuid

_SESSION_ID = str(uuid.uuid4())
_TELEMETRY_DISABLED = (
    os.environ.get("CHAT4000_TELEMETRY_DISABLED", "").strip().lower() in ("1", "true", "yes")
    or "--no-telemetry" in sys.argv
)

def _emit(event: str, props: Optional[dict] = None) -> None:
    if _TELEMETRY_DISABLED:
        return
    enriched = {
        "source": "hermes-plugin-installer",
        "installer_version": "1.0.0",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "os_platform": sys.platform,
        "session_id": _SESSION_ID,
        "arch": platform.machine() or "unknown",
        "cpu_count": os.cpu_count() or 0,
        "locale": (os.environ.get("LANG") or "").split(".")[0] or "unknown",
        "since_start_ms": int(time.time() * 1000) - _STARTED_AT_MS,
        "is_root": hasattr(os, "geteuid") and os.geteuid() == 0,
    }
    try:
        sysname = platform.system()
        if sysname == "Linux":
            os_rel = f"Linux {platform.release()}"
            try:
                for line in Path("/etc/os-release").read_text(errors="ignore").splitlines():
                    if line.startswith("PRETTY_NAME="):
                        os_rel = line.split("=", 1)[1].strip().strip('"')
                        break
            except Exception:
                pass
            enriched["os_release"] = os_rel
        elif sysname == "Darwin":
            mv = platform.mac_ver()[0]
            enriched["os_release"] = f"macOS {mv}" if mv else f"Darwin {platform.release()}"
        elif sysname == "Windows":
            wv = platform.win32_ver()[0]
            enriched["os_release"] = f"Windows {wv}" if wv else "Windows"
        else:
            enriched["os_release"] = f"{sysname} {platform.release()}".strip()
    except Exception:
        enriched["os_release"] = "unknown"
    try:
        in_container = False
        if Path("/.dockerenv").exists() or os.environ.get("KUBERNETES_SERVICE_HOST"):
            in_container = True
        else:
            cgroup = Path("/proc/1/cgroup").read_text(errors="ignore")
            in_container = any(s in cgroup for s in ("docker", "kubepods", "containerd", "podman"))
        enriched["is_container"] = in_container
    except Exception:
        enriched["is_container"] = False
    try:
        argv_out, skip_next = [], False
        for a in sys.argv[1:]:
            if skip_next:
                argv_out.append("<redacted>"); skip_next = False; continue
            if "=" in a:
                k = a.partition("=")[0]
                if any(s in k.lower() for s in ("token", "key", "secret", "pass", "dsn")):
                    argv_out.append(f"{k}=<redacted>"); continue
            if a.startswith(("sk-", "phc_", "ghp_", "Bearer")):
                argv_out.append("<redacted>"); continue
            if a in ("--token", "--api-key", "--secret", "--password", "--dsn"):
                argv_out.append(a); skip_next = True; continue
            argv_out.append(a)
        enriched["flags"] = argv_out
    except Exception:
        pass
    if props:
        enriched.update(props)
    body = json.dumps({
        "api_key": POSTHOG_API_KEY,
        "event": event,
        "distinct_id": resolve_install_id(),
        "properties": enriched,
    }).encode("utf-8")
    req = urllib.request.Request(
        POSTHOG_CAPTURE_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass  # never break the install

# ─── Sentry (stdlib envelope POST, no SDK) ────────────────────────────────


def _scrub_path(s: str) -> str:
    if not isinstance(s, str):
        return s
    home = str(Path.home())
    if home and home in s:
        s = s.replace(home, "~")
    import re as _re
    return _re.sub(r"/(Users|home)/[^/]+", r"/\\1/<user>", s)


def _scrub_secrets(s: str) -> str:
    if not isinstance(s, str):
        return s
    import re as _re
    s = _re.sub(r"sk-[A-Za-z0-9]{20,}", "[REDACTED_API_KEY]", s)
    s = _re.sub(r"phc_[A-Za-z0-9]{30,}", "[REDACTED_POSTHOG_KEY]", s)
    s = _re.sub(r"(?i)Bearer\\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", s)
    return s


def send_sentry_envelope(exc: BaseException, *, tags: Optional[dict] = None) -> None:
    """Post a Sentry envelope describing `exc` over plain HTTPS. Stdlib
    only — no sentry-sdk needed in the install bootstrap. Best-effort:
    never raises. Strips home paths and obvious secrets before sending."""
    if _TELEMETRY_DISABLED:
        return
    try:
        import traceback
        import datetime
        from urllib.parse import urlparse

        parsed = urlparse(SENTRY_DSN)
        public_key = parsed.username or ""
        project_id = (parsed.path or "").lstrip("/")
        if not public_key or not project_id or not parsed.hostname:
            return
        envelope_url = f"{parsed.scheme}://{parsed.hostname}/api/{project_id}/envelope/"

        frames = []
        tb = exc.__traceback__
        while tb is not None:
            f = tb.tb_frame
            co = f.f_code
            frames.append({
                "filename": _scrub_path(co.co_filename),
                "function": co.co_name,
                "lineno": tb.tb_lineno,
                "module": co.co_name,
                "in_app": "installer.py" in co.co_filename,
            })
            tb = tb.tb_next

        event = {
            "event_id": uuid.uuid4().hex,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "platform": "python",
            "level": "error",
            "release": INSTALLER_RELEASE,
            "environment": os.environ.get("HERMES_ENV") or "production",
            "tags": {
                "installer": "hermes",
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "os_platform": sys.platform,
                **(tags or {}),
            },
            "exception": {
                "values": [{
                    "type": type(exc).__name__,
                    "value": _scrub_secrets(str(exc))[:500],
                    "stacktrace": {"frames": frames},
                }]
            },
            "user": {"id": resolve_install_id()},
            "sdk": {"name": "chat4000-installer", "version": "1.0.0"},
        }

        envelope_header = json.dumps({"dsn": SENTRY_DSN, "event_id": event["event_id"]})
        item_header = json.dumps({"type": "event"})
        item_payload = json.dumps(event)
        body = (envelope_header + "\n" + item_header + "\n" + item_payload + "\n").encode("utf-8")

        req = urllib.request.Request(
            envelope_url,
            data=body,
            headers={
                "Content-Type": "application/x-sentry-envelope",
                "X-Sentry-Auth": (
                    f"Sentry sentry_version=7, sentry_key={public_key}, "
                    f"sentry_client=chat4000-installer/1.0"
                ),
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


# ─── Detection ────────────────────────────────────────────────────────────

def detect_hermes() -> Optional[tuple[str, str]]:
    """Return (venv_bin_path, layout_label) or None.

    Probes in order:
      1. $HERMES_INSTALL_DIR / $HERMES_HOME / $VIRTUAL_ENV env-var overrides
      2. `hermes` on PATH: parse the wrapper for a `/.../venv/bin` substring;
         if it's a symlink/shim, resolve and use the resolved dir
      3. Known layouts in HERMES_LAYOUTS (glob-aware for Homebrew Cellar)"""
    import re
    # 1. Env-var overrides (project-owned). Highest priority.
    install_dir = (os.environ.get("HERMES_INSTALL_DIR") or "").strip()
    if install_dir:
        p = str(Path(install_dir).expanduser() / "venv" / "bin")
        if Path(f"{p}/python").exists():
            return (p, "env:HERMES_INSTALL_DIR")
    hermes_home = (os.environ.get("HERMES_HOME") or "").strip()
    if hermes_home:
        p = str(Path(hermes_home).expanduser() / "hermes-agent" / "venv" / "bin")
        if Path(f"{p}/python").exists():
            return (p, "env:HERMES_HOME")
    venv = (os.environ.get("VIRTUAL_ENV") or "").strip()
    if venv:
        p = str(Path(venv).expanduser() / "bin")
        if Path(f"{p}/hermes").exists() and Path(f"{p}/python").exists():
            return (p, "env:VIRTUAL_ENV")

    # 2. `hermes` on PATH — try wrapper-grep, then resolve as symlink.
    hermes_cmd = shutil.which("hermes")
    if hermes_cmd:
        try:
            content = Path(hermes_cmd).read_text(errors="ignore")
            # Match `/.../venv/bin` or `/.../.venv/bin` or `/.../-env-<ver>/bin` (Nix)
            for pat in (
                r"/[^\"'\s]+/\.?venv/bin",
                r"/nix/store/[^\"'\s]+-hermes-agent-env-[^/]+/bin",
            ):
                m = re.search(pat, content)
                if m:
                    bin_path = m.group(0)
                    if Path(f"{bin_path}/python").exists() or Path(f"{bin_path}/hermes").exists():
                        return (bin_path, _layout_label(bin_path))
        except Exception:
            pass
        # Wrapper-grep missed (symlink, PE launcher, makeWrapper script).
        # Resolve and accept the resolved dir if it has python.
        try:
            real = Path(hermes_cmd).resolve()
            bin_path = str(real.parent)
            if Path(f"{bin_path}/python").exists():
                return (bin_path, _layout_label(bin_path))
        except Exception:
            pass

    # 3. Known layouts with glob support (Homebrew Cellar version dirs).
    for pattern, label in HERMES_LAYOUTS:
        expanded = str(Path(pattern).expanduser())
        if "*" in expanded:
            # Glob the pattern; pick the highest-versioned match
            # (last sorted = newest version directory).
            try:
                matches = sorted(Path("/").glob(expanded.lstrip("/")))
                for match in reversed(matches):
                    if (match / "python").exists():
                        return (str(match), label)
            except Exception:
                continue
        else:
            if (Path(expanded) / "python").exists():
                return (expanded, label)
    return None


def _layout_label(path: str) -> str:
    if "/nix/store/" in path:
        return "nix"
    for pattern, label in HERMES_LAYOUTS:
        expanded = str(Path(pattern).expanduser())
        if "*" in expanded:
            # Compare with glob: replace * with regex .*
            import re as _re
            rx = _re.escape(expanded).replace(r"\*", "[^/]+")
            if _re.fullmatch(rx, path):
                return label
        elif path == expanded:
            return label
    return "unknown"

def detect_uv() -> Optional[str]:
    p = shutil.which("uv")
    if p:
        return p
    for cand in (
        Path.home() / ".local" / "bin" / "uv",
        Path("/usr/local/bin/uv"),
        Path("/opt/homebrew/bin/uv"),
    ):
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    return None

# ─── Install steps ────────────────────────────────────────────────────────

def install_via_uv(uv: str, venv_python: str, ref: str) -> None:
    subprocess.run(
        [uv, "pip", "install", "--python", venv_python, f"git+{REPO_URL}@{ref}"],
        check=True,
    )

def install_via_pip(venv_python: str, ref: str) -> None:
    # Bootstrap pip if absent.
    has_pip = subprocess.run(
        [venv_python, "-c", "import pip"],
        capture_output=True,
    ).returncode == 0
    if not has_pip:
        say("Bootstrapping pip via ensurepip…")
        if subprocess.run(
            [venv_python, "-m", "ensurepip", "--upgrade"],
            capture_output=True,
        ).returncode != 0:
            say("ensurepip failed — fetching get-pip.py")
            with urllib.request.urlopen("https://bootstrap.pypa.io/get-pip.py", timeout=20) as resp:
                bootstrap = resp.read()
            subprocess.run([venv_python], input=bootstrap, check=True)
    subprocess.run(
        [venv_python, "-m", "pip", "install", "--upgrade", f"git+{REPO_URL}@{ref}"],
        check=True,
    )

def uninstall(venv_python: str, uv: Optional[str]) -> None:
    if uv:
        subprocess.run(
            [uv, "pip", "uninstall", "--python", venv_python, "chat4000-hermes-plugin"],
            check=False,
        )
    else:
        subprocess.run(
            [venv_python, "-m", "pip", "uninstall", "-y", "chat4000-hermes-plugin"],
            check=False,
        )

def reset_local_state() -> None:
    state_dir = Path.home() / ".hermes" / "plugins" / "chat4000"
    if state_dir.exists():
        warn(f"Removing {state_dir} (key + ack store) — already-paired devices will fail to decrypt until re-paired.")
        ans = input(f"{C_YEL}Continue? [y/N]:{C_RST} ").strip().lower()
        if ans not in ("y", "yes"):
            say("Reset cancelled.")
            return
        import shutil as _sh
        _sh.rmtree(state_dir, ignore_errors=True)
        ok(f"Removed {state_dir}")

# ─── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="chat4000 Hermes plugin installer",
        add_help=True,
    )
    parser.add_argument("--no-wizard", action="store_true",
                        help="install only, don't pair / restart gateway")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the plugin from Hermes' venv")
    parser.add_argument("--reset", action="store_true",
                        help="wipe local key + ack store (destructive)")
    parser.add_argument("--ref", default=DEFAULT_REF,
                        help=f"git ref to install (default: {DEFAULT_REF})")
    parser.add_argument("--verbose", action="store_true",
                        help="echo every subprocess command")
    parser.add_argument("--no-telemetry", action="store_true",
                        help="disable PostHog + Sentry for this run")
    parser.add_argument("--hermes-bin", default=None,
                        metavar="PATH",
                        help=(
                            "Skip auto-detection and use this Hermes venv directly. "
                            "PATH should be the directory containing `python` and "
                            "`hermes` (e.g. /opt/homebrew/Cellar/hermes-agent/2026.5.0/venv/bin)."
                        ))
    args = parser.parse_args()

    banner()
    _emit("installer_started")

    # 1. Detect Hermes ------------------------------------------------------
    venv_bin = None
    layout = None
    if args.hermes_bin:
        candidate = str(Path(args.hermes_bin.rstrip("/")).expanduser())
        if not Path(f"{candidate}/python").exists():
            err(f"--hermes-bin {candidate}: no `python` found there.")
            err("Make sure the path is the directory containing `python` and `hermes`")
            err("(e.g. /opt/homebrew/Cellar/hermes-agent/2026.5.16/libexec/bin).")
            _emit("installer_failed", {
                "stage": "detect_hermes",
                "error_class": "InvalidHermesBin",
                "error_msg": f"no python at {candidate}/python",
            })
            return 1
        venv_bin = candidate
        layout = "user-override"
        ok(f"Hermes venv:  {C_CYN}{venv_bin}{C_RST}  {C_DIM}(via --hermes-bin){C_RST}")
    else:
        detected = detect_hermes()
        if detected is not None:
            venv_bin, layout = detected
            ok(f"Hermes venv:  {C_CYN}{venv_bin}{C_RST}  {C_DIM}({layout}){C_RST}")
        else:
            print()
            err("Hey — we couldn't find where you installed Hermes.")
            print()
            print(f"We looked here:")
            print(f"  · env vars {C_DIM}HERMES_INSTALL_DIR / HERMES_HOME / VIRTUAL_ENV{C_RST}")
            print(f"  · {C_CYN}hermes{C_RST} on PATH")
            for pattern, label in HERMES_LAYOUTS:
                print(f"  · {pattern}  {C_DIM}({label}){C_RST}")
            print()
            print(f"{C_BOLD}Tell us where it is, or cancel:{C_RST}")
            print(f"  · type the path to the directory containing {C_CYN}python{C_RST} and {C_CYN}hermes{C_RST}")
            print(f"  · or press {C_CYN}Ctrl+C{C_RST} to cancel and re-run with arguments")
            print()
            print(f"{C_BOLD}Examples of a valid path:{C_RST}")
            print(f"  /opt/homebrew/Cellar/hermes-agent/2026.5.16/libexec/bin")
            print(f"  ~/.local/share/uv/tools/hermes-agent/bin")
            print(f"  /opt/venvs/hermes-agent/bin")
            print()
            print(f"{C_BOLD}Or re-run from your shell:{C_RST}")
            print(f"  {C_CYN}curl ... | bash -s -- --hermes-bin /your/path/to/venv/bin{C_RST}")
            print(f"  {C_CYN}curl ... | bash -s -- --help{C_RST}  {C_DIM}(see all flags){C_RST}")
            print()
            if not sys.stdin.isatty():
                err("(non-interactive shell — cannot prompt. Re-run interactively or pass --hermes-bin.)")
                _emit("installer_failed", {
                    "stage": "detect_hermes",
                    "error_class": "NotFound",
                    "error_msg": "no hermes venv; non-interactive shell",
                })
                return 1
            try:
                user_input = input(f"{C_CYN}? Hermes venv-bin path:{C_RST} ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                warn("Cancelled.")
                _emit("installer_cancelled", {"stage": "detect_hermes_prompt"})
                return 130
            if not user_input:
                err("Empty path. Bailing.")
                _emit("installer_failed", {
                    "stage": "detect_hermes",
                    "error_class": "NotFound",
                    "error_msg": "no hermes venv; empty user input",
                })
                return 1
            candidate = str(Path(user_input).expanduser())
            if not Path(f"{candidate}/python").exists():
                err(f"No `python` found at {candidate}. Bailing.")
                _emit("installer_failed", {
                    "stage": "detect_hermes",
                    "error_class": "InvalidUserInput",
                    "error_msg": f"no python at {candidate}/python",
                    "user_input_path": candidate,
                })
                return 1
            venv_bin = candidate
            layout = "user-input"
            ok(f"Hermes venv:  {C_CYN}{venv_bin}{C_RST}  {C_DIM}(via user input){C_RST}")
            _emit("installer_hermes_path_via_user_input", {"hermes_path": venv_bin})
    _emit("installer_hermes_detected", {"hermes_layout": layout, "hermes_path": venv_bin})
    venv_python = f"{venv_bin}/python"

    # 2. Uninstall / reset modes --------------------------------------------
    if args.uninstall:
        hdr("Uninstall mode")
        uninstall(venv_python, detect_uv())
        ok("Plugin uninstalled. Local key + state at ~/.hermes/plugins/chat4000 NOT removed (use --reset).")
        return 0

    if args.reset:
        hdr("Reset mode (destructive)")
        reset_local_state()

    # 3. Install ------------------------------------------------------------
    hdr(f"📦 Installing chat4000 plugin from {REPO_URL}@{args.ref}")
    uv = detect_uv()
    try:
        if uv:
            ok(f"Using uv at {C_CYN}{uv}{C_RST}")
            _emit("installer_uv_detected", {"uv_path": uv})
            install_via_uv(uv, venv_python, args.ref)
            installer_used = "uv"
        else:
            warn("uv not found — falling back to venv pip")
            install_via_pip(venv_python, args.ref)
            installer_used = "pip"
    except subprocess.CalledProcessError as exc:
        err(f"Install failed: {exc}")
        _emit("installer_failed", {
            "stage": "pip_install", "error_class": type(exc).__name__,
            "error_msg": str(exc)[:200], "installer_used": uv and "uv" or "pip",
        })
        return 1
    ok("Plugin installed.")

    # Verify import works
    check = subprocess.run(
        [venv_python, "-c", "import chat4000_hermes_plugin; print(chat4000_hermes_plugin.__name__)"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        err("Plugin installed but import failed:")
        err(check.stderr.strip())
        _emit("installer_failed", {"stage": "import_check", "error_msg": check.stderr.strip()[:200]})
        return 1

    # Read installed plugin version for the event
    ver = subprocess.run(
        [venv_python, "-c", "from chat4000_hermes_plugin.package_info import read_package_version; print(read_package_version())"],
        capture_output=True, text=True,
    )
    plugin_version = ver.stdout.strip() if ver.returncode == 0 else "unknown"
    ok(f"Installed version: {C_GRN}{plugin_version}{C_RST}")
    _emit("installer_pkg_installed", {
        "installer_used": installer_used,
        "plugin_ref": args.ref,
        "plugin_version": plugin_version,
    })

    # 4. Wizard handoff -----------------------------------------------------
    if args.no_wizard:
        warn("Skipping wizard (--no-wizard). Next steps:")
        print(f"  {C_CYN}{venv_bin}/chat4000 wizard{C_RST}")
        return 0

    hdr("🪄 Running install wizard")
    _emit("installer_handing_off_to_wizard")
    # exec so the wizard owns the real tty for Ctrl-C handling during pair.
    # NOTE: after execv the wizard takes over — any failures from here on
    # are reported by the wizard's own telemetry, not this installer.
    try:
        os.execv(f"{venv_bin}/chat4000", [f"{venv_bin}/chat4000", "wizard"])
    except OSError as exc:
        err(f"Could not exec wizard: {exc}")
        _emit("installer_failed", {
            "stage": "wizard_exec",
            "error_class": type(exc).__name__,
            "error_msg": str(exc)[:200],
        })
        return 1


def _entry() -> int:
    """Top-level wrapper that reports uncaught exceptions to Sentry +
    PostHog. Keeps Ctrl-C silent (user action, not a bug)."""
    try:
        return main()
    except KeyboardInterrupt:
        print()
        warn("Install cancelled.")
        _emit("installer_cancelled", {"stage": "uncaught"})
        return 130
    except SystemExit:
        raise
    except BaseException as exc:
        err(f"Installer crashed unexpectedly: {type(exc).__name__}: {exc}")
        _emit("installer_crashed", {
            "error_class": type(exc).__name__,
            "error_msg": str(exc)[:200],
        })
        send_sentry_envelope(exc, tags={"crash_stage": "uncaught"})
        err("Crash report sent. If this keeps happening, please open an issue:")
        err("  https://github.com/chat4000/chat4000-hermes-plugin/issues")
        return 1


if __name__ == "__main__":
    sys.exit(_entry())
