#!/usr/bin/env bash
# ARK — Automatic Research Kit
# One-click installer.
#
# Usage:
#   curl -fsSL https://idea2paper.org/install.sh | bash
#   curl -fsSL https://idea2paper.org/install.sh | bash -s -- --no-webapp
#   curl -fsSL https://idea2paper.org/install.sh -o install.sh && bash install.sh --help
#
# Flags:
#   --prefix DIR      Install ARK source to DIR (default: $HOME/ARK)
#   --branch NAME     Git branch/tag to clone (default: main)
#   --repo URL        Clone from URL (default: https://github.com/kaust-ark/ARK)
#   --no-webapp       Skip dashboard install (default: install + start systemd service)
#   --no-base         Skip building the per-project ark-base research env
#   --no-research     Skip the [research] extra (saves disk + an Anthropic dep)
#   --noninteractive  Skip the API-key + login prompts at the end
#   --dry-run         Print the plan, do not change anything
#   -h | --help       Show this help and exit
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
INSTALL_WEBAPP=1
SKIP_BASE=0
SKIP_RESEARCH=0
DRY_RUN=0
NONINTERACTIVE=0

usage() {
  # Print only the leading comment block (skip shebang, stop at first blank line).
  awk 'NR==1{next} /^[^#]/{exit} {sub(/^# ?/,""); print}' "$0"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)         PREFIX="$2"; shift 2 ;;
    --prefix=*)       PREFIX="${1#*=}"; shift ;;
    --branch)         BRANCH="$2"; shift 2 ;;
    --branch=*)       BRANCH="${1#*=}"; shift ;;
    --repo)           REPO_URL="$2"; shift 2 ;;
    --repo=*)         REPO_URL="${1#*=}"; shift ;;
    --webapp)         INSTALL_WEBAPP=1; shift ;;
    --no-webapp)      INSTALL_WEBAPP=0; shift ;;
    --no-base)        SKIP_BASE=1; shift ;;
    --no-research)    SKIP_RESEARCH=1; shift ;;
    --noninteractive) NONINTERACTIVE=1; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    -h|--help)        usage; exit 0 ;;
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
    # npm's shebang is `#!/usr/bin/env node`, so it needs `node` on PATH.
    # Without this, `npm install` fails: env: 'node': No such file or directory.
    PATH="$ARK_ENV_BIN:$PATH"; export PATH
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
# - Prepends the ark env's bin to PATH so subprocesses (claude, gemini) inherit
#   it even when the user runs `ark` from a shell without `conda activate`.
# - Sources ~/.ark/webapp.env so API keys (ANTHROPIC_API_KEY, GEMINI_API_KEY,
#   CLAUDE_CODE_OAUTH_TOKEN) flow into the orchestrator's environment for CLI
#   runs. The webapp service gets the same file via systemd EnvironmentFile.
SHIM_DIR="${HOME}/.local/bin"
SHIM="$SHIM_DIR/ark"
# Use the source dir's .ark/ — this is what get_config_dir() resolves to
# inside the running webapp/CLI, so writing here means keys propagate to
# both the systemd unit (via EnvironmentFile=) and to interactive Settings
# loads. ~/.ark/ would silently diverge from where the webapp reads.
ARK_CONFIG_DIR="${PREFIX}/.ark"
ARK_ENV_FILE="${ARK_CONFIG_DIR}/webapp.env"
if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$SHIM_DIR" "$ARK_CONFIG_DIR"
  ARK_ENV_BIN="$(dirname "$ARK_PY")"
  cat > "$SHIM" <<EOF
#!/usr/bin/env bash
export PATH="$ARK_ENV_BIN:\$PATH"
if [ -f "$ARK_ENV_FILE" ]; then
  set -a
  while IFS='=' read -r _k _v; do
    case "\$_k" in ''|\\#*) continue;; esac
    eval "\$_k=\"\$_v\""
  done < "$ARK_ENV_FILE"
  set +a
  unset _k _v
fi
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

# ─── 5. Interactive setup: API keys + login email ──────────────────────
# Prompts read from /dev/tty so they work even when the script is curl-piped
# (stdin is the script content). Skip silently if no controlling terminal
# (CI runs, ssh -T, docker exec without -t) or --noninteractive was passed.
upsert_env() {
  # upsert_env KEY VALUE  →  set/replace `KEY=VALUE` in $ARK_ENV_FILE
  local k="$1" v="$2"
  [ -z "$v" ] && return 0
  touch "$ARK_ENV_FILE"
  if grep -q "^${k}=" "$ARK_ENV_FILE" 2>/dev/null; then
    # Replace existing line (use # as sed delimiter so / in keys is fine).
    sed -i.bak "s#^${k}=.*#${k}=${v}#" "$ARK_ENV_FILE" && rm -f "${ARK_ENV_FILE}.bak"
  else
    printf '%s=%s\n' "$k" "$v" >> "$ARK_ENV_FILE"
  fi
  chmod 600 "$ARK_ENV_FILE"
}

# Env-var fallbacks for automation/CI: set ARK_GEMINI_KEY,
# ARK_CLAUDE_OAUTH, ARK_LOGIN_EMAIL to pre-fill any of the three answers.
# Anything not set falls through to an interactive prompt (if a TTY is
# attached) or is silently skipped under --noninteractive.
LOGIN_EMAIL="${ARK_LOGIN_EMAIL:-}"
GKEY="${ARK_GEMINI_KEY:-}"
CKEY="${ARK_CLAUDE_OAUTH:-}"
INTERACTIVE_OK=0
[ "$DRY_RUN" -eq 0 ] && [ "$NONINTERACTIVE" -eq 0 ] && [ -e /dev/tty ] && [ -r /dev/tty ] && INTERACTIVE_OK=1

if [ "$INTERACTIVE_OK" -eq 1 ] && \
   { [ -z "$GKEY" ] || [ -z "$CKEY" ] || [ -z "$LOGIN_EMAIL" ]; }; then
  step "Configure API keys (press Enter to skip any prompt)"
  if [ -z "$GKEY" ]; then
    printf '   Gemini API key   (https://aistudio.google.com/apikey): '
    IFS= read -r GKEY < /dev/tty || GKEY=""
  fi
  if [ -z "$CKEY" ]; then
    printf '   Claude OAuth token (sk-ant-oat01-...) or Enter to use `claude` browser flow: '
    IFS= read -r CKEY < /dev/tty || CKEY=""
  fi
  if [ -z "$LOGIN_EMAIL" ]; then
    printf '   Email for dashboard login: '
    IFS= read -r LOGIN_EMAIL < /dev/tty || LOGIN_EMAIL=""
  fi
  say
fi

if [ "$DRY_RUN" -eq 0 ]; then
  upsert_env GEMINI_API_KEY "$GKEY"
  upsert_env GOOGLE_API_KEY "$GKEY"
  [ -n "$CKEY" ] && upsert_env CLAUDE_CODE_OAUTH_TOKEN "$CKEY"
  # SECRET_KEY signs magic-link tokens. Without an explicit value in
  # webapp.env, every Python process generates a fresh random secret →
  # tokens minted by `ark webapp login` won't verify against the running
  # webapp's secret. Pin one here, BEFORE installing the service, so all
  # processes share it. Re-running install.sh leaves an existing key in
  # place (upsert_env replaces existing keys; we only set if missing).
  if ! grep -q "^SECRET_KEY=" "$ARK_ENV_FILE" 2>/dev/null; then
    upsert_env SECRET_KEY "$("$ARK_PY" -c 'import secrets; print(secrets.token_hex(32))')"
  fi
fi

# ─── 6. Webapp service ────────────────────────────────────────────────
if [ "$INSTALL_WEBAPP" -eq 1 ]; then
  step "Installing webapp as systemd user service"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$ARK_PY" -m ark.cli webapp install || warn "webapp install reported errors (see output above)."
  else
    run "$ARK_PY" -m ark.cli webapp install
  fi
fi

# ─── 7. Onboarding ─────────────────────────────────────────────────────
say
say "${C_GREEN}${C_BOLD}ARK installed.${C_RESET}"
say

# If --webapp + email captured, generate a magic link and tell the user to
# click it. This is the "open the URL once and you're in" path that bypasses
# SMTP entirely — perfect for self-host where smtp.gmail.com isn't configured.
if [ "$DRY_RUN" -eq 0 ] && [ "$INSTALL_WEBAPP" -eq 1 ] && [ -n "$LOGIN_EMAIL" ]; then
  step "Sign in to the dashboard"
  "$ARK_PY" -m ark.cli webapp login "$LOGIN_EMAIL" || \
    warn "Could not generate magic link (webapp may not have started yet — try \`ark webapp login $LOGIN_EMAIL\` after a moment)."
fi

say "${C_BOLD}Next steps${C_RESET}"
if [ "$INSTALL_WEBAPP" -eq 1 ]; then
  say "  1. ${C_BOLD}Open the dashboard${C_RESET} (click the magic link printed above, or):"
  say "       ${C_CYAN}http://localhost:${WEBAPP_PORT}${C_RESET}"
  say "       Manage with: ${C_DIM}ark webapp status | restart | logs -f${C_RESET}"
  say
  say "  2. Or use the CLI:"
else
  say "  1. Use the CLI:"
fi
say "       ${C_CYAN}ark doctor${C_RESET}                # verify install"
say "       ${C_CYAN}ark new myproject${C_RESET}         # interactive wizard"
say "       ${C_CYAN}ark run  myproject${C_RESET}"
say "       ${C_CYAN}ark monitor myproject${C_RESET}"
say
say "  ${C_DIM}Keys saved to: ${ARK_ENV_FILE}${C_RESET}"
say "  ${C_DIM}Re-run \`ark webapp login <email>\` anytime to get a fresh sign-in link.${C_RESET}"
say
say "Docs:    https://idea2paper.org/doc.html"
say "Source:  $PREFIX"
say
