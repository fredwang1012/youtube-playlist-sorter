"""The deploy kit for the Oracle VM: systemd units, setup.sh (runs on the VM) and push.sh (runs on the laptop).

Nothing here touches systemd, SSH or the VM; the files are read as text.
"""
import re
from pathlib import Path

import pytest

from app import auth
from app.config import Settings
from app.web import auth as web_auth

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
CREDENTIALS = ("refresh-token", "login-client-secret", "session-key")
SANDBOX = {
    "NoNewPrivileges": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "PrivateTmp": "yes",
    "ReadWritePaths": "/var/lib/ytps",
    "RestrictAddressFamilies": "AF_INET AF_INET6 AF_UNIX",
    "MemoryDenyWriteExecute": "yes",
}
SERVICES = ("ytps.service", "ytps-run.service")


def text(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def unit(name: str) -> dict[str, dict[str, list[str]]]:
    """A systemd unit as {section: {key: [values in order]}}; keys may repeat, unlike configparser's INI."""
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    for line in text(name).splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        assert current is not None, f"{name}: key outside a section: {line}"
        key, _, value = line.partition("=")
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


def env_of(service: dict[str, list[str]]) -> dict[str, str]:
    return dict(item.split("=", 1) for value in service.get("Environment", []) for item in value.split())


def setting_names() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


# --- units ---

def test_credential_names_match_the_code():
    assert {auth.REFRESH_TOKEN, web_auth.LOGIN_CLIENT_SECRET, web_auth.SESSION_KEY} == set(CREDENTIALS)


@pytest.mark.parametrize("name", SERVICES)
def test_units_have_sandboxing_and_credentials(name):
    service = unit(name)["Service"]
    assert service["User"] == ["ytps"] and service["Group"] == ["ytps"]
    assert service["EnvironmentFile"] == ["/etc/ytps/env"]
    env = env_of(service)
    assert env["SECRETS_BACKEND"] == "systemd"
    assert env["DATA_DIR"] == "/var/lib/ytps"
    assert env["GRAPH_PATH"] == "/var/lib/ytps/mine.yaml"  # the UI edits the file the timer runs
    assert {k for k in env if not k.startswith(("UV_", "PYTHON"))} <= setting_names()
    assert sorted(service["LoadCredentialEncrypted"]) == sorted(f"{c}:/etc/ytps/creds/{c}.cred" for c in CREDENTIALS)
    for key, value in SANDBOX.items():
        assert service[key] == [value], key
    assert "AF_NETLINK" not in service["RestrictAddressFamilies"][0]


def test_serve_and_run_commands():
    serve = unit("ytps.service")["Service"]["ExecStart"]
    run = unit("ytps-run.service")["Service"]["ExecStart"]
    assert len(serve) == len(run) == 1
    assert re.fullmatch(r"/usr/local/bin/uv run .*python -m app\.cli serve", serve[0])
    assert re.fullmatch(r"/usr/local/bin/uv run .*python -m app\.cli run --graph /var/lib/ytps/mine\.yaml", run[0])
    assert "--no-sync" in serve[0] and "--no-sync" in run[0]  # the code directory is read-only to the service
    assert unit("ytps-run.service")["Service"]["Type"] == ["oneshot"]
    assert unit("ytps.service")["Install"]["WantedBy"] == ["multi-user.target"]
    assert "Install" not in unit("ytps-run.service")  # started only by the timer


def test_services_share_credentials_and_sandbox():
    shared = ("User", "Group", "EnvironmentFile", "Environment", "LoadCredentialEncrypted", "WorkingDirectory", *SANDBOX)
    serve, run = unit("ytps.service")["Service"], unit("ytps-run.service")["Service"]
    for key in shared:
        assert sorted(serve.get(key, [])) == sorted(run.get(key, [])), key


def test_timer_every_6h_persistent():
    timer = unit("ytps-run.timer")
    assert timer["Timer"]["OnCalendar"] == ["*-*-* 00/6:00:00"]
    assert timer["Timer"]["Persistent"] == ["true"]
    assert timer["Timer"]["RandomizedDelaySec"] == ["10m"]
    assert timer["Timer"].get("Unit", ["ytps-run.service"]) == ["ytps-run.service"]
    assert timer["Install"]["WantedBy"] == ["timers.target"]


# --- scripts ---

SECRET_VARS = r"(value|secret|token|key|password|\w*SECRET\w*|\w*TOKEN\w*)"


@pytest.mark.parametrize("name", ["setup.sh", "push.sh"])
def test_scripts_are_strict_and_never_print_secrets(name):
    script = text(name)
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "\nset -euo pipefail\n" in script
    assert not re.search(r"^\s*set -[a-z]*x", script, re.M), "tracing would print secrets"
    assert not re.search(rf"\becho\b[^\n]*\$\{{?{SECRET_VARS}\b", script, re.I)
    assert not re.search(rf"\bprintf\b[^|\n]*\$\{{?{SECRET_VARS}\b[^|\n]*$", script, re.I | re.M), \
        "printf of a secret is only allowed straight into a pipe"


def test_setup_script_creates_user_and_never_echoes_secrets():
    script = text("setup.sh")
    assert "\nAPP_USER=ytps\n" in script
    assert re.search(r"useradd --system\b[^;]*\"\$APP_USER\"", script)
    assert "/var/lib/ytps" in script and "/etc/ytps/creds" in script
    # hidden prompt, then the value goes to systemd-creds on stdin ("-"), never on its command line
    assert re.search(r"read -r?s", script) or "systemd-ask-password" in script
    assert re.search(r'printf \'%s\' "\$value" \| systemd-creds encrypt --name="\$name" - ', script)
    assert "| systemd-creds encrypt --name=session-key - " in script  # generated on the VM, never shown
    for credential in CREDENTIALS:
        assert credential in script
    assert "systemd-creds setup" in script
    assert re.search(r"\b250\b", script), "LoadCredentialEncrypted needs systemd 250"


def test_setup_env_template_uses_real_settings():
    script = text("setup.sh")
    template = re.search(r"<<'ENV'\n(.*?)\nENV\n", script, re.S)
    assert template, "setup.sh writes /etc/ytps/env from a quoted heredoc"
    keys = {line.split("=", 1)[0].lstrip("# ") for line in template.group(1).splitlines()
            if re.match(r"#? ?[A-Z_]+=", line)}
    assert {"PUBLIC_URL", "AUTH_MODE", "LOGIN_CLIENT_ID", "OWNER_TAILSCALE_LOGIN"} <= keys
    assert keys <= setting_names()


def test_push_excludes_data_and_git():
    script = text("push.sh")
    for pattern in (".git", "./data", "node_modules", ".venv", "./.env", "__pycache__", "./graphs/mine.yaml",
                    "client_secret*.json", "token*.json", "./.[!.]*"):
        assert f"--exclude='{pattern}'" in script, pattern
    assert "npm run build" in script
    assert "app/web/static/index.html" in script  # refuses to ship without the built UI
    assert re.search(r"uv sync --frozen --no-dev", script, re.I)  # "$REMOTE_UV sync": the lock decides, no resolve
    assert "systemctl try-restart" in script
    assert "sudo -n" in script  # fail fast instead of a password prompt fighting the piped stdin


def test_push_moves_refresh_token_on_stdin():
    script = text("push.sh")
    block = re.search(r"\nrefresh_token\(\) \{\n(.*?)\n\}\n", script, re.S).group(1)
    assert "| ssh" in block and "setup.sh secret refresh-token" in block
    assert "secretstore.get(auth.REFRESH_TOKEN)" in block


@pytest.mark.parametrize("name", ["ytps.service", "ytps-run.service", "ytps-run.timer", "setup.sh", "push.sh"])
def test_lf_line_endings(name):
    assert b"\r\n" not in (DEPLOY / name).read_bytes()
