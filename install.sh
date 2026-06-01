#!/usr/bin/env bash
#
# install.sh — minimal Python bootstrap. Finds a working Python ≥ 3.8
# and hands off to scripts/installer.py which does EVERYTHING ELSE
# (detect Hermes, install plugin, run wizard, restart gateway, fire
# analytics).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/chat4000/chat4000-hermes-plugin/stable/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/chat4000/chat4000-hermes-plugin/stable/install.sh | bash -s -- --no-wizard
#
# Testing an unreleased commit (before tagging `stable`): set CHAT4000_INSTALL_REF
# to a branch/tag/commit SHA. install.sh then fetches installer.py from THAT ref
# AND installs the plugin from it (so a changed installer.py is exercised too):
#   curl -fsSL https://raw.githubusercontent.com/chat4000/chat4000-hermes-plugin/<SHA>/install.sh \
#     | CHAT4000_INSTALL_REF=<SHA> bash
# An explicit `--ref` flag still overrides the install ref.
#
# Stage servers: pass `--stage` (or set CHAT4000_ENV=stage, inherited by the
# installer + wizard) to onboard/pair against the stage registrar + gateway:
#   curl -fsSL .../<SHA>/install.sh | CHAT4000_INSTALL_REF=<SHA> bash -s -- --stage
#
# All flags pass through to installer.py. See `bash install.sh --help`
# (after fetching) for the full list.

set -euo pipefail

# Ref to install from: a branch, tag, or commit SHA. Defaults to the stable tag.
REF="${CHAT4000_INSTALL_REF:-stable}"
REPO_RAW="https://raw.githubusercontent.com/chat4000/chat4000-hermes-plugin/${REF}"

# Find a usable Python interpreter (≥ 3.8).
find_python() {
  for cand in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' 2>/dev/null; then
        printf "%s" "$cand"
        return 0
      fi
    fi
  done
  return 1
}

PY="$(find_python || true)"
if [[ -z "$PY" ]]; then
  printf "\033[1;31m✗\033[0m Need Python ≥ 3.8 on PATH. Install Python first, then re-run.\n" >&2
  exit 1
fi

# Download + run installer.py. Use process substitution to keep argv
# intact (`bash -c "curl ... | python"` would lose them).
TMP="$(mktemp -t chat4000-installer.XXXXXX.py)"
trap 'rm -f "$TMP"' EXIT
curl -fsSL "$REPO_RAW/scripts/installer.py" -o "$TMP"

# Pass the same ref to installer.py so the pip install matches the installer we
# just fetched — unless the caller passed their own --ref (then theirs wins).
case " $* " in
  *" --ref "*) exec "$PY" "$TMP" "$@" ;;
  *)           exec "$PY" "$TMP" --ref "$REF" "$@" ;;
esac
