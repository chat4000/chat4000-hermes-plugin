#!/usr/bin/env bash
#
# install.sh — install the chat4000 Hermes plugin and run the
# interactive wizard.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/chat4000/chat4000-hermes-plugin/stable/install.sh | bash
#
#   # or after cloning:
#   ./install.sh                  # full install: install + wizard
#   ./install.sh --no-wizard      # install only, don't pair / restart gw
#   ./install.sh --uninstall      # remove the plugin from Hermes' venv
#   ./install.sh --reset          # wipe local key + ack store (destructive)
#   ./install.sh --ref REF        # install from a specific git ref
#   ./install.sh --verbose        # set -x
#   ./install.sh --log FILE       # tee all output to FILE
#   ./install.sh --help
#
# Works on Linux + macOS. Detects both common Hermes install layouts
# (Docker /usr/local/lib/hermes-agent and curl-installer ~/.hermes),
# uses uv when available, falls back to pip via ensurepip / get-pip.py.

set -euo pipefail

# ─── Defaults ────────────────────────────────────────────────────────────
REPO="https://github.com/chat4000/chat4000-hermes-plugin"
REF="stable"
DO_WIZARD=1
DO_UNINSTALL=0
DO_RESET=0
LOG_FILE=""

# ─── Args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-wizard)  DO_WIZARD=0;    shift ;;
    --uninstall)  DO_UNINSTALL=1; shift ;;
    --reset)      DO_RESET=1;     shift ;;
    --ref)        REF="$2";       shift 2 ;;
    --verbose)    set -x;         shift ;;
    --log)        LOG_FILE="$2";  shift 2 ;;
    -h|--help)    sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1 (try --help)" >&2; exit 2 ;;
  esac
done

if [[ -n "$LOG_FILE" ]]; then
  exec > >(tee -a "$LOG_FILE") 2>&1
fi

# ─── Pretty output ───────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_RED=$'\033[1;31m'
  C_GRN=$'\033[1;32m'
  C_YEL=$'\033[1;33m'
  C_BLU=$'\033[1;34m'
  C_MAG=$'\033[1;35m'
  C_CYN=$'\033[1;36m'
  C_DIM=$'\033[2m'
  C_RST=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_MAG=""; C_CYN=""; C_DIM=""; C_RST=""
fi

say()  { printf "%s>%s %s\n" "$C_CYN" "$C_RST" "$*"; }
ok()   { printf "%s✓%s %s\n" "$C_GRN" "$C_RST" "$*"; }
warn() { printf "%s⚠%s %s\n" "$C_YEL" "$C_RST" "$*"; }
err()  { printf "%s✗%s %s\n" "$C_RED" "$C_RST" "$*" >&2; }
hdr()  {
  printf "\n%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" "$C_MAG" "$C_RST"
  printf "%s%s%s\n" "$C_MAG" "$1" "$C_RST"
  printf "%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n\n" "$C_MAG" "$C_RST"
}

banner() {
  printf "\n"
  printf "%s┌─────────────────────────────────────────────────────────────┐%s\n" "$C_MAG" "$C_RST"
  printf "%s│%s  %s🔐 chat4000%s  ·  %sHermes plugin installer%s                       %s│%s\n" "$C_MAG" "$C_RST" "$C_MAG" "$C_RST" "$C_BLU" "$C_RST" "$C_MAG" "$C_RST"
  printf "%s│%s  %sNative iPhone / Mac / CLI app for your Hermes agent%s        %s│%s\n" "$C_MAG" "$C_RST" "$C_DIM" "$C_RST" "$C_MAG" "$C_RST"
  printf "%s└─────────────────────────────────────────────────────────────┘%s\n" "$C_MAG" "$C_RST"
  printf "\n"
}

# ─── Hermes venv detection ───────────────────────────────────────────────
detect_hermes() {
  local cmd
  cmd="$(command -v hermes 2>/dev/null || true)"
  if [[ -n "$cmd" ]]; then
    # First try grep-ing the wrapper for an explicit venv path.
    local from_wrapper
    from_wrapper="$(grep -oE '/[^"\"]+/venv/bin' "$cmd" 2>/dev/null | head -1 || true)"
    if [[ -n "$from_wrapper" && -x "$from_wrapper/python" ]]; then
      printf "%s" "$from_wrapper"
      return 0
    fi
  fi
  # Fallback — the two known install layouts.
  for p in \
    /usr/local/lib/hermes-agent/venv/bin \
    "$HOME/.hermes/hermes-agent/venv/bin"
  do
    if [[ -x "$p/python" ]]; then
      printf "%s" "$p"
      return 0
    fi
  done
  return 1
}

# ─── uv detection ────────────────────────────────────────────────────────
detect_uv() {
  command -v uv 2>/dev/null && return 0
  for p in "$HOME/.local/bin/uv" /usr/local/bin/uv /opt/homebrew/bin/uv; do
    [[ -x "$p" ]] && printf "%s" "$p" && return 0
  done
  return 1
}

# ─── Run banner ──────────────────────────────────────────────────────────
banner

HERMES_BIN="$(detect_hermes || true)"
if [[ -z "$HERMES_BIN" ]]; then
  err "Could not locate Hermes venv. Looked at:"
  err "  - PATH (\`command -v hermes\` failed or wrapper didn't reveal venv)"
  err "  - /usr/local/lib/hermes-agent/venv/bin (Docker / FHS layout)"
  err "  - ~/.hermes/hermes-agent/venv/bin (curl-installer layout)"
  err ""
  err "Install Hermes first, then re-run this script."
  exit 1
fi
ok "Hermes venv:  ${C_CYN}${HERMES_BIN}${C_RST}"

# ─── Uninstall mode ──────────────────────────────────────────────────────
if [[ "$DO_UNINSTALL" == 1 ]]; then
  hdr "Uninstall mode"
  UV_CMD="$(detect_uv || true)"
  if [[ -n "$UV_CMD" ]]; then
    "$UV_CMD" pip uninstall --python "$HERMES_BIN/python" chat4000-hermes-plugin || true
  else
    "$HERMES_BIN/python" -m pip uninstall -y chat4000-hermes-plugin || true
  fi
  ok "Plugin uninstalled."
  warn "Local key + ack store at ~/.hermes/plugins/chat4000 NOT removed."
  warn "Use \`--reset\` (separately) to wipe local state."
  exit 0
fi

# ─── Reset mode ──────────────────────────────────────────────────────────
if [[ "$DO_RESET" == 1 ]]; then
  hdr "Reset mode (destructive)"
  warn "About to delete ~/.hermes/plugins/chat4000 (key + ack store)."
  warn "Already-paired devices will fail to decrypt until re-paired."
  printf "%sContinue? [y/N]:%s " "$C_YEL" "$C_RST"
  read -r ans
  case "$ans" in
    [yY]|[yY][eE][sS])
      rm -rf "$HOME/.hermes/plugins/chat4000"
      ok "Removed ~/.hermes/plugins/chat4000"
      ;;
    *) say "Reset cancelled."; exit 0 ;;
  esac
fi

# ─── Step 1 / N: install plugin ──────────────────────────────────────────
hdr "📦 Installing chat4000 plugin from ${REPO}@${REF}"

UV_CMD="$(detect_uv || true)"
if [[ -n "$UV_CMD" ]]; then
  ok "Using uv at ${C_CYN}${UV_CMD}${C_RST}"
  "$UV_CMD" pip install --python "$HERMES_BIN/python" "git+${REPO}@${REF}"
else
  warn "uv not found — falling back to venv pip"
  if ! "$HERMES_BIN/python" -c 'import pip' 2>/dev/null; then
    say "Bootstrapping pip via ensurepip…"
    if ! "$HERMES_BIN/python" -m ensurepip --upgrade 2>/dev/null; then
      say "ensurepip failed — fetching get-pip.py"
      curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$HERMES_BIN/python"
    fi
  fi
  "$HERMES_BIN/python" -m pip install --upgrade "git+${REPO}@${REF}"
fi
ok "Plugin installed."

# ─── Verify installation ─────────────────────────────────────────────────
if ! "$HERMES_BIN/python" -c 'import chat4000_hermes_plugin' 2>/dev/null; then
  err "Plugin installed but `import chat4000_hermes_plugin` failed."
  err "Check the install output above for errors."
  exit 1
fi
INSTALLED_VERSION="$("$HERMES_BIN/python" -c 'from chat4000_hermes_plugin.package_info import read_package_version; print(read_package_version())')"
ok "Installed version: ${C_GRN}${INSTALLED_VERSION}${C_RST}"

# ─── Step 2 / N: run the interactive wizard ──────────────────────────────
if [[ "$DO_WIZARD" == 1 ]]; then
  hdr "🪄 Running install wizard"
  # exec so the wizard's signal handling (Ctrl-C in pair) sees the real
  # tty without bash sitting in between.
  exec "$HERMES_BIN/chat4000" wizard
else
  warn "Skipping wizard (--no-wizard). Next steps:"
  echo "  ${C_CYN}${HERMES_BIN}/chat4000 wizard${C_RST}"
  echo "  ${C_DIM}(or: chat4000 pair  +  pkill hermes gateway ; nohup hermes gateway run …)${C_RST}"
fi
