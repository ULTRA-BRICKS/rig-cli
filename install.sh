#!/usr/bin/env bash
# install.sh — install the `rig` CLI (Python 3).
# Works from a local clone (./install.sh) and piped from curl:
#   curl -fsSL https://git.hyperide.ai/ultrabricks/rig-cli/raw/branch/main/install.sh | bash
set -euo pipefail

# ── identity ──────────────────────────────────────────────────────────────────
TOOL="rig"
REPO="rig-cli"
FORGE="https://git.hyperide.ai/ultrabricks"   # canonical home; github.com/alex-mextner is an archived mirror
ENTRY="bin/rig"   # path inside repo root
CLONE_BASE="${XDG_DATA_HOME:-$HOME/.local/share}"
EXPECT_URL="$FORGE/$REPO.git"
DIST="hyper-rig"   # PyPI distribution name

# ── Windows (Cygwin / MSYS2 / Git Bash) ───────────────────────────────────────
# There python, git and uv are native Windows programs: a symlink to the entry script (a
# `#!/usr/bin/env python3` file) cannot run, and Cygwin's own ~/.local/bin is not the one on
# PATH. `uv tool install` puts a real <tool>.exe into %USERPROFILE%\.local\bin, which IS on PATH
# in every Windows shell — so on Windows that is the whole install.
case "$(uname -s)" in
  CYGWIN*|MINGW*|MSYS*)
    if ! command -v uv >/dev/null 2>&1; then
      echo "ERROR: on Windows $TOOL installs through uv, and uv is not on PATH." >&2
      echo "       Install it:  winget install astral-sh.uv   (or: powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\")" >&2
      exit 1
    fi
    _with=(); [[ -n "${UV_WITH:-}" ]] && _with=(--with "$UV_WITH")  # one argument, spaces and all
    _here=""
    if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "bash" ]]; then
      _here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    fi
    if [[ -n "$_here" && -f "$_here/$ENTRY" ]]; then
      _src="$_here"; command -v cygpath >/dev/null 2>&1 && _src="$(cygpath -w "$_here")"
      echo "$TOOL: Windows — uv tool install --editable $_src"
      uv tool install --force --editable "$_src" ${_with[@]+"${_with[@]}"}
    else
      echo "$TOOL: Windows — uv tool install git+${EXPECT_URL%.git}"
      uv tool install --force "git+${EXPECT_URL%.git}" ${_with[@]+"${_with[@]}"}
    fi
    _bindir="$(uv tool dir --bin 2>/dev/null || true)"
    if [[ -n "$_bindir" ]] && command -v cygpath >/dev/null 2>&1; then _bindir="$(cygpath -u "$_bindir")"; fi
    if [[ -n "$_bindir" && ":$PATH:" != *":$_bindir:"* ]]; then
      echo ""
      echo "  NOTE: $_bindir (where uv put $TOOL.exe) is not on PATH — run 'uv tool update-shell'"
      echo "        or add it to PATH, then open a new shell."
    fi
    PYTHONUTF8=1 "${_bindir:+$_bindir/}$TOOL" install-skill \
      || echo "  WARNING: '$TOOL install-skill' failed — re-run it once $TOOL is on PATH."
    echo ""
    if [[ -n "${_src:-}" ]]; then _upd="git pull in $_here"; else _upd="uv tool upgrade $DIST"; fi
    echo "  $TOOL is installed (uv tool). Update: $_upd"
    exit 0
    ;;
esac

# Origins that are the same repo: the canonical Forgejo one and the pre-migration GitHub ones (a
# GitHub origin is re-pointed at Forgejo — the GitHub copy is frozen). Compared as HOST + PATH, never
# as a substring: an origin that merely contains "hyperide.ai" (git.hyperide.ai.evil.example,
# evil.example/git.hyperide.ai/…) must not pass for the canonical repo and get pulled + installed.
_url_host_path() {  # https / ssh / scp-style git URL -> "host path" (user@, :port, .git, trailing / dropped)
  _u=${1%/}; _u=${_u%.git}
  case "$_u" in
    https://*|ssh://*) _u=${_u#*://}; _hp=${_u%%/*}; _path=${_u#*/} ;;
    *@*:*) _hp=${_u%%:*}; _path=${_u#*:} ;;
    *) return 1 ;;
  esac
  [ "$_path" != "$_u" ] || return 1
  _host=${_hp##*@}; _host=${_host%%:*}
  printf '%s %s\n' "$_host" "$_path"
}
_origin_is_canonical() {
  [ "$(_url_host_path "$1")" = "git.hyperide.ai ultrabricks/$REPO" ]
}
_origin_is_legacy() {
  case "$(_url_host_path "$1")" in
    "github.com alex-mextner/$REPO"|"github.com ultrabricks/$REPO") return 0 ;;
  esac
  return 1
}

# ── locate source dir ─────────────────────────────────────────────────────────
_script_dir=""
if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "bash" ]]; then
  _script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ -n "$_script_dir" && -f "$_script_dir/$ENTRY" ]]; then
  SRC="$_script_dir"
  echo "rig: using local clone at $SRC"
else
  mkdir -p "$CLONE_BASE"
  CLONE_DIR="$CLONE_BASE/$REPO"
  if [[ -d "$CLONE_DIR/.git" ]]; then
    actual_url="$(git -C "$CLONE_DIR" remote get-url origin 2>/dev/null || echo "")"
    if _origin_is_legacy "$actual_url"; then
      echo "rig: $REPO moved to $FORGE — re-pointing origin ($actual_url -> $EXPECT_URL)"
      git -C "$CLONE_DIR" remote set-url origin "$EXPECT_URL"
    elif ! _origin_is_canonical "$actual_url"; then
      echo "ERROR: $CLONE_DIR exists but its origin is '$actual_url', not $EXPECT_URL." >&2
      echo "       Remove that directory or fix its remote, then re-run." >&2
      exit 1
    fi
    echo "rig: updating existing clone at $CLONE_DIR"
    git -C "$CLONE_DIR" pull --ff-only
  else
    echo "rig: cloning $EXPECT_URL into $CLONE_DIR"
    git clone "$EXPECT_URL" "$CLONE_DIR"
  fi
  SRC="$CLONE_DIR"
fi

# ── bin dir ───────────────────────────────────────────────────────────────────
BIN="$HOME/.local/bin"
mkdir -p "$BIN"

if [[ ":$PATH:" != *":$BIN:"* ]]; then
  echo ""
  echo "  NOTE: $BIN is not on your PATH."
  echo "  Add this to your ~/.bashrc or ~/.zshrc and restart your shell:"
  echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
  echo ""
fi

# ── core runtime dependencies ─────────────────────────────────────────────────
# pyyaml, textual, and rich are all CORE deps (not optional extras). Install them
# into the SAME interpreter rig runs under. Prefer `uv` (the toolchain rig users
# standardize on) over a bare `pip`; fall back to `python3 -m pip --user` only when uv is
# absent. NOTE: this is best-effort and non-fatal — on an externally-managed system Python
# (PEP-668) BOTH uv and pip refuse a system-wide install, so the warning below fires and
# `rig doctor` / a managed (uv-tool) install is the real fix.
# textual floor is 0.66, matching pyproject.toml and riglib/doctor.py's
# `_TEXTUAL_MIN_VERSION` (see their comments for the full rig-cli#292 story — the effective
# floor is the max of every new Textual API surface introduced: `refresh_bindings()` needs
# 0.63, `Button(tooltip=...)` needs 0.66). This check is presence-only (`import textual`
# succeeds on ANY installed version), so it only fires — and only THEN installs the correct
# >=0.66 floor — when textual is genuinely absent; a present-but-older textual is caught
# instead by `riglib.cli._tui_importable()`'s version-aware gate at `rig init` time, which
# degrades to a non-destructive preview + an upgrade command rather than launching a wizard
# that crashes.
_missing_core=()
python3 -c 'import yaml' 2>/dev/null     || _missing_core+=("pyyaml")
python3 -c 'import textual' 2>/dev/null  || _missing_core+=("textual>=0.66")
python3 -c 'import rich' 2>/dev/null     || _missing_core+=("rich>=13")
if [[ "${#_missing_core[@]}" -gt 0 ]]; then
  py3="$(command -v python3)"
  # Shell-quote each package name for DISPLAY (printf %q), not a bare space-joined "${arr[*]}"
  # — "textual>=0.66" echoed unquoted has a bare `>`, which is a redirection operator when
  # copy-pasted into a shell (silently truncates the package list AND creates a stray file
  # named "=0.66"). The EXECUTED commands below already use safe array expansion
  # ("${_missing_core[@]}"); this only fixes what a human reads and might copy-paste — the
  # same hazard `_dim_cmd` in riglib/cli.py exists to close on the Python side of this diff.
  pkgs=$(printf '%q ' "${_missing_core[@]}")
  pkgs="${pkgs% }"  # strip the trailing separator printf's loop leaves behind
  if command -v uv >/dev/null 2>&1; then
    echo "rig: core deps missing, attempting: uv pip install --python $py3 $pkgs"
    uv pip install --python "$py3" "${_missing_core[@]}" 2>/dev/null && _core_ok=1
  else
    echo "rig: core deps missing, attempting: python3 -m pip install --user $pkgs"
    python3 -m pip install --user "${_missing_core[@]}" 2>/dev/null && _core_ok=1
  fi
  if [[ "${_core_ok:-0}" != "1" ]]; then
    echo ""
    echo "  WARNING: could not install core deps ($pkgs)."
    echo "  rig init TUI wizard and config parsing will fail until they are present."
    echo "  A common cause: this Python is PEP-668 externally-managed (modern Homebrew/distro"
    echo "  Python refuses a plain install on purpose) — but the failure above could also be a"
    echo "  network/permission/resolver issue unrelated to that. Run \`rig doctor\` (or \`rig"
    echo "  doctor --yes\`, which tries the plain command first and then PRINTS — never"
    echo "  auto-runs — a --break-system-packages fallback for a local dev checkout like this"
    echo "  one, IF that's actually why it failed) and follow what it reports for THIS machine."
    echo ""
  fi
fi

# ── symlink entry ─────────────────────────────────────────────────────────────
ENTRY_PATH="$SRC/$ENTRY"
chmod +x "$ENTRY_PATH"
ln -sfn "$ENTRY_PATH" "$BIN/$TOOL"
echo "rig: symlinked $BIN/$TOOL -> $ENTRY_PATH"

# ── register skill ────────────────────────────────────────────────────────────
if ! "$BIN/$TOOL" install-skill; then
  echo "  WARNING: '$TOOL install-skill' failed — $TOOL is installed but agents may not"
  echo "           auto-discover it. Re-run '$TOOL install-skill' manually to fix."
fi

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  rig is installed."
echo "  Usage: rig doctor              — check/install dependencies"
echo "         rig init                — onboarding: scaffold rig.yaml + preview (wizard/--yes; --apply to apply)"
echo "         rig apply               — PREVIEW the reconcile (alias for 'rig apply info')"
echo "         rig apply commit        — execute the reconcile (idempotent — what applies)"
echo "         rig status              — report config↔disk drift (both ways)"
echo "         rig config get|set      — read/change one key (dot path), then reconcile"
echo "         rig env set|list|del   — machine-wide env.vars keys (values masked; plaintext files)"
echo "         rig --help              — full usage"
echo ""
