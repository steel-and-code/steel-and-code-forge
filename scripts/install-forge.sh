#!/usr/bin/env bash
# Install Steel & Code Forge for the current user, without sudo.
set -euo pipefail
umask 077

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN_HOME="$HOME/.local/bin"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
INSTALL_DIR="$DATA_HOME/steel-and-code/forge"
CONFIG_DIR="$CONFIG_HOME/steel-and-code/forge"
KEY_FILE="$CONFIG_DIR/openrouter.key"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$BIN_HOME"
chmod 700 "$CONFIG_DIR"
echo "Installing Forge and Workbench into $INSTALL_DIR/venv"
if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/python" -m pip install --upgrade pip
"$INSTALL_DIR/venv/bin/python" -m pip install "$SOURCE_DIR[workbench]"

# This launcher loads the per-user credential only into the Forge process.
# Do not source the credential file: treat its contents as data, not shell code.
cat >"$BIN_HOME/forge" <<'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail
umask 077
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
VENV_FORGE="$DATA_HOME/steel-and-code/forge/venv/bin/forge"
CONFIG_DIR="$CONFIG_HOME/steel-and-code/forge"
KEY_FILE="$CONFIG_DIR/openrouter.key"

if [[ "${1:-}" == "setup-key" ]]; then
  mkdir -p "$CONFIG_DIR"
  chmod 700 "$CONFIG_DIR"
  if [[ ! -t 0 ]]; then
    echo "Run forge setup-key in an interactive terminal." >&2
    exit 2
  fi
  read -r -s -p "OpenRouter API key (input hidden): " api_key
  printf '\n'
  if [[ -z "$api_key" || "$api_key" == *$'\n'* || "$api_key" == *$'\r'* ]]; then
    echo "Key cannot be empty or contain newlines." >&2
    exit 2
  fi
  temporary="$(mktemp "$CONFIG_DIR/.key.XXXXXXXX")"
  trap 'rm -f -- "$temporary"' EXIT
  printf '%s\n' "$api_key" >"$temporary"
  chmod 600 "$temporary"
  mv -f -- "$temporary" "$KEY_FILE"
  trap - EXIT
  unset api_key
  echo "OpenRouter key saved to your private user configuration."
  exit 0
fi

if [[ ! -x "$VENV_FORGE" ]]; then
  echo "Forge is not installed. Run scripts/install-forge.sh from its source directory." >&2
  exit 1
fi
if [[ -z "${FORGE_OPENROUTER_API_KEY:-}" && -r "$KEY_FILE" ]]; then
  IFS= read -r FORGE_OPENROUTER_API_KEY <"$KEY_FILE" || true
  export FORGE_OPENROUTER_API_KEY
fi
exec "$VENV_FORGE" "$@"
LAUNCHER
chmod 700 "$BIN_HOME/forge"

if [[ ! -s "$KEY_FILE" ]]; then
  echo "Set up your key with: $BIN_HOME/forge setup-key"
  if [[ -t 0 ]]; then
    "$BIN_HOME/forge" setup-key
  fi
else
  echo "Existing OpenRouter key preserved. To change it: forge setup-key"
fi

echo
echo "Forge installation complete."
echo "Command: $BIN_HOME/forge"
echo "Version: $("$BIN_HOME/forge" --version)"
if [[ ":$PATH:" != *":$BIN_HOME:"* ]]; then
  echo "Add $BIN_HOME to PATH (for example, in ~/.bashrc or ~/.zshrc)."
fi
echo 'Launch: forge tui --workspace /path/to/project'
echo 'Browser: forge workbench --workspace /path/to/project'
