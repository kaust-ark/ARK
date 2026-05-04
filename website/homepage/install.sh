#!/usr/bin/env bash
# ARK — Automatic Research Kit
# One-click installer.
#
# Usage:
#   curl -fsSL https://idea2paper.org/install.sh | bash
#   curl -fsSL https://idea2paper.org/install.sh | bash -s -- --webapp
#   curl -fsSL https://idea2paper.org/install.sh -o install.sh && bash install.sh --help
#
# Flags:
#   --prefix DIR     Install ARK source to DIR (default: $HOME/ARK)
#   --branch NAME    Git branch/tag to clone (default: main)
#   --repo URL       Clone from URL (default: https://github.com/kaust-ark/ARK)
#   --webapp         Install + start the dashboard as a systemd user service (Linux)
#   --no-base        Skip building the per-project ark-base research env
#   --no-research    Skip the [research] extra (saves disk + an Anthropic dep)
#   --dry-run        Print the plan, do not change anything
#   -h | --help      Show this help and exit
#
# Notes:
#   * Installs miniforge3 to ~/miniforge3 if no conda is detected.
#   * Creates two conda envs: ark-base (per-project research stack)
#     and ark (where the ark CLI lives, pip install -e).
#   * The webapp service runs on port 9527 in the user's systemd manager.
#     Disable with: systemctl --user stop ark-webapp.
#   * Refuses to run as root (use a regular user; conda will be in $HOME).

set -eu

# ─── Constants ─────────────────────────────────────────────────────────
DEFAULT_REPO="https://github.com/kaust-ark/ARK"
DEFAULT_BRANCH="main"
DEFAULT_PREFIX="${HOME}/ARK"
MINIFORGE_DIR="${HOME}/miniforge3"
WEBAPP_PORT=9527

# ─── Args ──────────────────────────────────────────────────────────────
PREFIX="${ARK_PREFIX:-$DEFAULT_PREFIX}"
BRANCH="${ARK_BRANCH:-$DEFAULT_BRANCH}"
REPO_URL="${ARK_REPO:-$DEFAULT_REPO}"
INSTALL_WEBAPP=0
SKIP_BASE=0
SKIP_RESEARCH=0
DRY_RUN=0

usage() {
  # Print only the leading comment block (skip shebang, stop at first blank line).
  awk 'NR==1{next} /^[^#]/{exit} {sub(/^# ?/,""); print}' "$0"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)        PREFIX="$2"; shift 2 ;;
    --prefix=*)      PREFIX="${1#*=}"; shift ;;
    --branch)        BRANCH="$2"; shift 2 ;;
    --branch=*)      BRANCH="${1#*=}"; shift ;;
    --repo)          REPO_URL="$2"; shift 2 ;;
    --repo=*)        REPO_URL="${1#*=}"; shift ;;
    --webapp)        INSTALL_WEBAPP=1; shift ;;
    --no-base)       SKIP_BASE=1; shift ;;
    --no-research)   SKIP_RESEARCH=1; shift ;;
    --dry-run)       DRY_RUN=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) printf 'Unknown flag: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

# ─── Pretty printing ──────────────────────────────────────────────────
if [ -t 1 ]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_CYAN=$'\033[36m'; C_RESET=$'\033[0m'
else
  C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_CYAN=""; C_RESET=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s>> %s%s\n' "$C_BOLD" "$*" "$C_RESET"; }
warn() { printf '%s!! %s%s\n' "$C_YELLOW" "$*" "$C_RESET" >&2; }
fail() { printf '%sxx %s%s\n' "$C_RED" "$*" "$C_RESET" >&2; exit 1; }
note() { printf '   %s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '%s$ %s%s\n' "$C_CYAN" "$*" "$C_RESET"
  else
    printf '%s$ %s%s\n' "$C_CYAN" "$*" "$C_RESET"
    "$@"
  fi
}

# ─── Pre-flight ────────────────────────────────────────────────────────
[ "$(id -u)" = "0" ] && fail "Don't run as root — use a regular user account (conda lives in \$HOME)."

OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
  Linux)  PLATFORM=linux;  MINIFORGE_OS=Linux ;;
  Darwin) PLATFORM=macos;  MINIFORGE_OS=MacOSX ;;
  *) fail "Unsupported OS: $OS (Linux and macOS only). On Windows, use WSL2." ;;
esac

case "$ARCH" in
  x86_64|amd64) MINIFORGE_ARCH=x86_64 ;;
  arm64|aarch64) MINIFORGE_ARCH=arm64 ;;
  *) fail "Unsupported arch: $ARCH" ;;
esac

if [ "$INSTALL_WEBAPP" -eq 1 ] && [ "$PLATFORM" != "linux" ]; then
  warn "--webapp uses systemd; ignoring on $PLATFORM (you can run \`ark webapp\` in the foreground instead)."
  INSTALL_WEBAPP=0
fi
if [ "$INSTALL_WEBAPP" -eq 1 ] && ! command -v systemctl >/dev/null 2>&1; then
  warn "--webapp needs systemctl, not found in PATH (containers, WSL1, etc.). Skipping; \`ark webapp\` still works in the foreground."
  INSTALL_WEBAPP=0
fi

for cmd in git curl bash; do
  command -v "$cmd" >/dev/null 2>&1 || fail "Missing required tool: $cmd"
done

step "ARK installer — $PLATFORM/$MINIFORGE_ARCH"
note "prefix:        $PREFIX"
note "branch:        $BRANCH"
note "repo:          $REPO_URL"
note "research deps: $([ $SKIP_RESEARCH -eq 1 ] && echo no || echo yes)"
note "ark-base env:  $([ $SKIP_BASE -eq 1 ] && echo skip || echo create)"
note "webapp svc:    $([ $INSTALL_WEBAPP -eq 1 ] && echo yes || echo no)"
[ "$DRY_RUN" -eq 1 ] && note "MODE:          dry-run (no changes)"
say

# ─── 1. Conda ──────────────────────────────────────────────────────────
step "Locating conda"
CONDA_BIN=""
if command -v conda >/dev/null 2>&1; then
  CONDA_BIN="$(command -v conda)"
  note "Found existing conda: $CONDA_BIN"
elif [ -x "$MINIFORGE_DIR/bin/conda" ]; then
  CONDA_BIN="$MINIFORGE_DIR/bin/conda"
  note "Found existing miniforge: $CONDA_BIN"
else
  step "Installing miniforge3 to $MINIFORGE_DIR"
  url="https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-${MINIFORGE_OS}-${MINIFORGE_ARCH}.sh"
  tmp="$(mktemp -t miniforge.XXXXXX.sh)"
  trap 'rm -f "$tmp"' EXIT
  run curl -fsSL -o "$tmp" "$url"
  run bash "$tmp" -b -p "$MINIFORGE_DIR"
  CONDA_BIN="$MINIFORGE_DIR/bin/conda"
fi

# Make `conda` callable in this script (without polluting user's shell).
if [ "$DRY_RUN" -eq 0 ]; then
  # shellcheck disable=SC1091
  . "$(dirname "$CONDA_BIN")/../etc/profile.d/conda.sh"
fi

# ─── 2. ARK source ─────────────────────────────────────────────────────
step "Fetching ARK source"
if [ -d "$PREFIX/.git" ]; then
  note "Existing repo at $PREFIX — fetching latest on $BRANCH"
  run git -C "$PREFIX" fetch --tags origin
  run git -C "$PREFIX" checkout "$BRANCH"
  run git -C "$PREFIX" pull --ff-only origin "$BRANCH" || warn "Could not fast-forward; leaving as-is"
elif [ -e "$PREFIX" ]; then
  fail "$PREFIX exists and is not an ARK git checkout. Move/remove it or pass --prefix DIR."
else
  run git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$PREFIX"
fi

# Submodules (PaperBanana etc.) — best-effort so a missing submodule never
# blocks the core CLI install.
if [ "$DRY_RUN" -eq 0 ]; then
  ( cd "$PREFIX" && git submodule update --init --recursive 2>/dev/null ) || \
    warn "Submodule init skipped — figure pipeline may fall back to nano-banana."
fi

# ─── 3. ark-base env (per-project research stack) ─────────────────────
if [ "$SKIP_BASE" -eq 0 ]; then
  step "Creating per-project research env: ark-base"
  if [ "$DRY_RUN" -eq 0 ] && conda env list | awk '{print $1}' | grep -qx 'ark-base'; then
    note "ark-base already exists — skipping"
  else
    if [ "$PLATFORM" = "macos" ] && [ -f "$PREFIX/environment-macos.yml" ]; then
      env_file="$PREFIX/environment-macos.yml"
    else
      env_file="$PREFIX/environment.yml"
    fi
    run conda env create -f "$env_file"
  fi
else
  note "Skipping ark-base (--no-base). \`ark new\` will fail until you create it."
fi

# ─── 4. ark env (CLI + webapp) ─────────────────────────────────────────
step "Creating ark env (CLI + webapp)"
if [ "$DRY_RUN" -eq 0 ] && conda env list | awk '{print $1}' | grep -qx 'ark'; then
  note "ark env already exists — reusing"
else
  run conda create -n ark -y python=3.11 pip
fi

ARK_PY="$([ "$DRY_RUN" -eq 0 ] && conda run -n ark which python || echo '<conda>/envs/ark/bin/python')"

step "Installing ARK package (pip install -e)"
extras="webapp"
[ "$SKIP_RESEARCH" -eq 0 ] && extras="${extras},research"
run "$ARK_PY" -m pip install --upgrade pip
run "$ARK_PY" -m pip install -e "$PREFIX[$extras]"

# ─── 4b. Agent CLIs (Claude Code, Gemini) ─────────────────────────────
# ARK invokes `claude` and `gemini` via subprocess. We install both into
# the ark conda env (via Node.js from conda-forge) so they live alongside
# the python that runs ark.cli, no system root required. Skip individually
# if the user already has them on $PATH.
if [ "$DRY_RUN" -eq 0 ]; then
  ARK_ENV_BIN="$(dirname "$ARK_PY")"
  need_claude=1
  need_gemini=1
  command -v claude >/dev/null 2>&1 && need_claude=0
  command -v gemini >/dev/null 2>&1 && need_gemini=0

  if [ "$need_claude" -eq 1 ] || [ "$need_gemini" -eq 1 ]; then
    step "Installing Node.js (for agent CLIs)"
    if [ ! -x "$ARK_ENV_BIN/npm" ]; then
      run conda install -n ark -y -c conda-forge "nodejs>=20"
    else
      note "Node.js already in ark env"
    fi
    if [ "$need_claude" -eq 1 ]; then
      step "Installing Claude Code CLI"
      run "$ARK_ENV_BIN/npm" install -g @anthropic-ai/claude-code
    fi
    if [ "$need_gemini" -eq 1 ]; then
      step "Installing Gemini CLI"
      run "$ARK_ENV_BIN/npm" install -g @google/gemini-cli
    fi
  fi
fi

# Create user-level shim so `ark` is on PATH without activating the env.
# Prepend the ark env's bin to PATH so subprocesses (claude, gemini) inherit
# a working PATH even when the user runs `ark` from a shell without conda
# activated.
SHIM_DIR="${HOME}/.local/bin"
SHIM="$SHIM_DIR/ark"
if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$SHIM_DIR"
  ARK_ENV_BIN="$(dirname "$ARK_PY")"
  cat > "$SHIM" <<EOF
#!/usr/bin/env bash
export PATH="$ARK_ENV_BIN:\$PATH"
exec "$ARK_PY" -m ark.cli "\$@"
EOF
  chmod +x "$SHIM"
  step "Installed launcher: $SHIM"
  case ":$PATH:" in
    *":$SHIM_DIR:"*) ;;
    *) warn "$SHIM_DIR is not on \$PATH. Add this to ~/.bashrc or ~/.zshrc:"
       printf '       export PATH="%s:$PATH"\n' "$SHIM_DIR" ;;
  esac
fi

# ─── 5. Optional webapp service ───────────────────────────────────────
if [ "$INSTALL_WEBAPP" -eq 1 ]; then
  step "Installing webapp as systemd user service"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$ARK_PY" -m ark.cli webapp install || warn "webapp install reported errors (see output above)."
  else
    run "$ARK_PY" -m ark.cli webapp install
  fi
fi

# ─── 6. Onboarding ─────────────────────────────────────────────────────
say
say "${C_GREEN}${C_BOLD}ARK installed.${C_RESET}"
say
say "${C_BOLD}Next steps${C_RESET}"
say "  1. Authenticate the agent CLIs (one-time):"
say "       ${C_CYAN}claude${C_RESET}      ${C_DIM}# launches Claude Code, sign in (or set ANTHROPIC_API_KEY)${C_RESET}"
say "       ${C_CYAN}gemini${C_RESET}      ${C_DIM}# launches Gemini CLI, sign in${C_RESET}"
say "     For Deep Research also set: ${C_DIM}export GEMINI_API_KEY=...${C_RESET}"
say
say "  2. Verify the install:"
say "       ${C_CYAN}ark doctor${C_RESET}"
say
say "  3. Create your first project (interactive wizard):"
say "       ${C_CYAN}ark new myproject${C_RESET}"
say "       ${C_CYAN}ark run  myproject${C_RESET}"
say "       ${C_CYAN}ark monitor myproject${C_RESET}"
say
if [ "$INSTALL_WEBAPP" -eq 1 ]; then
  say "  4. Open the dashboard:"
  say "       ${C_CYAN}http://localhost:${WEBAPP_PORT}${C_RESET}"
  say "       Manage with: ${C_DIM}ark webapp status | restart | logs -f${C_RESET}"
else
  say "  4. (Optional) Start the dashboard on demand:"
  say "       ${C_CYAN}ark webapp${C_RESET}                 # foreground, port ${WEBAPP_PORT}"
  say "       ${C_CYAN}ark webapp install${C_RESET}         # systemd user service (Linux)"
fi
say
say "Docs:    https://idea2paper.org/doc.html"
say "Source:  $PREFIX"
say
