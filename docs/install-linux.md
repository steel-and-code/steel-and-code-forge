# Install Forge on Linux (including Steel OS)

The installer uses the current `feature/workbench` packaging, including the
Workbench optional dependencies. Requires Python 3.11+, `venv`, pip and internet
access for Python dependencies. Run as your normal user, **not** with sudo.

```bash
git clone https://github.com/steel-and-code/steel-and-code-forge.git
cd steel-and-code-forge
bash scripts/install-forge.sh
```

If the installer has not yet been merged into the default branch, check out the
packaging branch first. The repository must be accessible to the installer.

During setup, paste your personal [OpenRouter](https://openrouter.ai/) API key
when asked. Terminal input is hidden. An existing key is preserved on upgrades.

Run from any directory:

```bash
forge --version
forge setup-key                            # add or replace your key
forge tui --workspace ~/Projects/my-project
forge workbench --workspace ~/Projects/my-project
```

If `forge` is not found, add `$HOME/.local/bin` to your PATH and restart the
shell (or invoke `~/.local/bin/forge` directly).

## Installation locations

- App environment: `${XDG_DATA_HOME:-~/.local/share}/steel-and-code/forge/venv`
- Launcher: `~/.local/bin/forge`
- User secret: `${XDG_CONFIG_HOME:-~/.config}/steel-and-code/forge/openrouter.key`

The credential directory is mode 0700 and the key is mode 0600. The key is
outside the workspace and source tree, and is not included in the package.
The launcher reads the file as plain data, never as shell code. The existing
`FORGE_OPENROUTER_API_KEY` environment variable takes precedence.

## Notes

This is a **per-user source-based installer**, not yet an RPM, Flatpak, Steel OS
ISO integration, or Windows installer. Installing from the private repository
requires access to it; a public distribution needs a published package/release.
The project deliberately requires explicit workspace selection for agent runs.
Forge safety approval rules remain unchanged. Keep OpenRouter keys private.

## Optional Hermes Agent

On an interactive installation, the script asks whether to install the separate
Nous Research Hermes Agent. The default is **no**. Saying yes downloads the
[official Hermes installer](https://hermes-agent.nousresearch.com/docs/getting-started/installation/)
using HTTPS and executes it as the current user. Review the upstream installer
if you do not trust its code; its dependencies and privileges are controlled by
its upstream project, not Forge. An existing Hermes installation is preserved.

Hermes may ask for its own provider/model settings. To configure it later:

```bash
~/.local/bin/hermes model
```

Forge's OpenRouter key is not automatically copied to Hermes. The installer offers to launch `hermes model` so you can configure OpenRouter directly. For a valid
Hermes install and model setup, try `forge hermes --workspace /path/to/project
"Say hello"` from a shell with `~/.local/bin` on PATH. Hermes remains an
external tool and Forge does not control its internal execution permissions.

## Test release for T and Uncle Claude

**Linux only; private preview.** Testers need GitHub access to the private repository, Python 3.11+ with venv/pip, git, and curl if choosing Hermes. Do not share personal keys. No keys are included in this release.

```bash
git clone --branch feature/forge-user-installer https://github.com/steel-and-code/steel-and-code-forge.git
cd steel-and-code-forge
bash -n scripts/install-forge.sh
bash scripts/install-forge.sh
~/.local/bin/forge --version
~/.local/bin/forge tui --workspace "$HOME/Projects"
```

If `~/Projects` does not exist, make a project directory and point `--workspace` to it. To try Hermes, choose its optional installer, configure OpenRouter using `hermes model`, then run `forge hermes --workspace "$HOME/Projects" "Say hello"`.

The preview installer downloads the upstream Hermes install script if opted into; read and approve third-party installer behavior before running. Only execute on a trusted test machine. A successful response is the smoke test; failures should be reported with **secrets redacted**. This preview has not been validated on fresh machines yet.
