"""Static acceptance checks for the per-user Linux installer."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-forge.sh"


def test_installer_uses_optional_workbench_dependencies():
    script = INSTALLER.read_text(encoding="utf-8")
    assert '"$SOURCE_DIR[workbench]"' in script
    assert "python3 -m venv" in script


def test_installer_avoids_sudo_and_sources_no_secret():
    script = INSTALLER.read_text(encoding="utf-8")
    assert "sudo " not in script.replace("without sudo", "")
    assert 'source "$KEY_FILE"' not in script
    assert 'read -r -s -p' in script
    assert 'chmod 600 "$temporary"' in script
    assert 'chmod 700 "$CONFIG_DIR"' in script


def test_installer_provides_key_rotation_command():
    script = INSTALLER.read_text(encoding="utf-8")
    assert '"setup-key"' in script
    assert 'FORGE_OPENROUTER_API_KEY' in script
    assert 'exec "$VENV_FORGE" "$@"' in script
