#!/usr/bin/env bash
# Prepares the VM for youtube-playlist-sorter and imports its secrets. Runs on the VM as root; see docs/REMOTE.md.
#
#   sudo bash setup.sh                 user ytps, directories, uv and Python 3.12, /etc/ytps/env template
#   sudo bash setup.sh secret NAME     encrypt one secret (refresh-token or login-client-secret) from a hidden prompt,
#                                      or from stdin when piped, into /etc/ytps/creds/NAME.cred
#   sudo bash setup.sh session-key     generate a random session-key credential (signs sign-in cookies)
#   sudo bash setup.sh check           confirm every credential decrypts and the env file is filled in
#
# Secrets only ever travel on stdin into systemd-creds; they are never echoed, logged or passed as arguments.
set -euo pipefail

APP_USER=ytps
DATA_DIR=/var/lib/ytps
ETC_DIR=/etc/ytps
CRED_DIR=/etc/ytps/creds
PYTHON_DIR=/opt/ytps-python
# uv matches the laptop's version (the lock file's format follows it); the hashes are the release's own .sha256 files
UV_VERSION=0.13.5
declare -A UV_SHA256=(
  [aarch64]=4bb69cf95ed13f23713bb7bc09f241896ea0c75b7c557e615a20604b82f9c403
  [x86_64]=f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f
)
CREDENTIALS=(refresh-token login-client-secret session-key)

die() {
  printf 'setup.sh: %s\n' "$*" >&2
  exit 1
}

need_root() {
  [[ $EUID -eq 0 ]] || die "run it with sudo"
}

check_systemd() {
  if [[ ${YTPS_PLAIN_CREDENTIALS:-0} == 1 ]]; then
    printf 'setup.sh: YTPS_PLAIN_CREDENTIALS=1: skipping the systemd 250 check; see docs/REMOTE.md\n' >&2
    return
  fi
  local version
  version=$(systemctl --version | awk 'NR == 1 { print $2 }')
  version=${version%%.*}
  [[ $version =~ ^[0-9]+$ ]] || die "cannot read the systemd version"
  # LoadCredentialEncrypted= and 'systemd-creds' arrived in systemd 250
  ((version >= 250)) || die "systemd $version is too old; encrypted credentials need systemd 250 or newer"
}

install_uv() {
  if [[ -x /usr/local/bin/uv ]] && [[ $(/usr/local/bin/uv --version) == "uv $UV_VERSION "* ]]; then
    return
  fi
  command -v curl >/dev/null || die "curl is required to install uv"
  local arch tmp
  arch=$(uname -m)
  [[ -n ${UV_SHA256[$arch]:-} ]] || die "no pinned uv build for $arch"
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/uv.tar.gz"     "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$arch-unknown-linux-gnu.tar.gz"
  printf '%s  %s
' "${UV_SHA256[$arch]}" "$tmp/uv.tar.gz" | sha256sum --check --quiet || die "uv download checksum mismatch"
  tar -xzf "$tmp/uv.tar.gz" -C "$tmp" --strip-components=1
  install -m 0755 -o root -g root "$tmp/uv" /usr/local/bin/uv
  rm -rf "$tmp"
}

write_env_template() {
  [[ -e $ETC_DIR/env ]] && return
  install -m 0640 -o root -g "$APP_USER" /dev/null "$ETC_DIR/env"
  cat >"$ETC_DIR/env" <<'ENV'
# Settings for ytps.service and ytps-run.service. No secrets in this file: those are in /etc/ytps/creds.
# After editing: sudo systemctl restart ytps

# The HTTPS address tailscale serve gives this VM, exactly as the browser shows it, with no trailing slash.
# Every change (tick, save, Check now) is refused unless the browser's Origin equals it.
PUBLIC_URL=https://VM-NAME.TAILNET.ts.net

# google: Google sign-in for OWNER_EMAIL only. tailscale: the fallback, trusting tailscale serve's identity header.
AUTH_MODE=google

# Client ID of the Web application OAuth client (public; its secret is the login-client-secret credential).
LOGIN_CLIENT_ID=

# Required when AUTH_MODE=google: the one Google account allowed to sign in.
OWNER_EMAIL=

# Only for AUTH_MODE=tailscale: the owner's Tailscale login, e.g. name@github (see docs/REMOTE.md).
OWNER_TAILSCALE_LOGIN=

# DAILY_QUOTA_BUDGET=8000
# TICK_QUOTA_CEILING=9500
# BIND_PORT=8765
ENV
}

install_all() {
  need_root
  check_systemd
  command -v tar >/dev/null || die "tar is required"
  if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home \
      --shell "$(command -v nologin || echo /usr/sbin/nologin)" "$APP_USER"
  fi
  install -d -m 0755 -o root -g root /opt/ytps "$PYTHON_DIR" /opt/ytps-venv
  install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"
  install -d -m 0750 -o root -g "$APP_USER" "$ETC_DIR"
  install -d -m 0700 -o root -g root "$CRED_DIR"
  install_uv
  UV_PYTHON_INSTALL_DIR=$PYTHON_DIR /usr/local/bin/uv python install 3.12
  if [[ ${YTPS_PLAIN_CREDENTIALS:-0} != 1 ]]; then
    systemd-creds setup  # makes the host key that encrypts the credentials, if there is none yet
  fi
  write_env_template
  printf 'Done. Next: run deploy/push.sh on the laptop, then import the secrets (docs/REMOTE.md).\n'
}

import_secret() {
  need_root
  local name=${1:-} value=""
  case $name in
    refresh-token | login-client-secret) ;;
    session-key) die "the session key is generated on the VM: sudo bash setup.sh session-key" ;;
    *) die "usage: setup.sh secret refresh-token|login-client-secret" ;;
  esac
  [[ -d $CRED_DIR ]] || die "$CRED_DIR is missing; run 'sudo bash setup.sh' first"
  if [[ -t 0 ]]; then
    read -rsp "Paste $name (input hidden), then press Enter: " value
    printf '\n' >&2
  else
    IFS= read -r value || [[ -n $value ]] # piped from the laptop, maybe without a final newline
  fi
  value=${value//[$'\r\n\t ']/}
  [[ -n $value ]] || die "empty input; nothing saved"
  printf '%s' "$value" | systemd-creds encrypt --name="$name" - "$CRED_DIR/$name.cred.new"
  value=""
  chmod 0600 "$CRED_DIR/$name.cred.new"
  mv -f "$CRED_DIR/$name.cred.new" "$CRED_DIR/$name.cred"
  printf 'Saved %s (encrypted). Restart to use it: sudo systemctl restart ytps\n' "$name"
}

new_session_key() {
  need_root
  [[ -d $CRED_DIR ]] || die "$CRED_DIR is missing; run 'sudo bash setup.sh' first"
  # 32 random bytes as URL-safe text; a new key signs everyone out
  head -c 32 /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_' | systemd-creds encrypt --name=session-key - "$CRED_DIR/session-key.cred.new"
  chmod 0600 "$CRED_DIR/session-key.cred.new"
  mv -f "$CRED_DIR/session-key.cred.new" "$CRED_DIR/session-key.cred"
  printf 'Saved a new session-key (encrypted). Restart to use it: sudo systemctl restart ytps\n'
}

check() {
  need_root
  local name ok=1
  if grep -q '^AUTH_MODE=tailscale' "$ETC_DIR/env"; then
    printf 'note     AUTH_MODE=tailscale: login-client-secret is only a placeholder the unit needs to start
'
  fi
  for name in "${CREDENTIALS[@]}"; do
    if systemd-creds decrypt --name="$name" "$CRED_DIR/$name.cred" - >/dev/null 2>&1; then
      printf 'ok       %s\n' "$name"
    else
      printf 'MISSING  %s (or it does not decrypt)\n' "$name"
      ok=0
    fi
  done
  if grep -q '^PUBLIC_URL=https://VM-NAME' "$ETC_DIR/env" || ! grep -q '^PUBLIC_URL=https://' "$ETC_DIR/env"; then
    printf 'NOT SET  PUBLIC_URL in %s\n' "$ETC_DIR/env"
    ok=0
  fi
  [[ -f $ETC_DIR/client_secret.json ]] || { printf 'MISSING  %s (deploy/push.sh HOST desktop-client)\n' "$ETC_DIR/client_secret.json"; ok=0; }
  ((ok)) || exit 1
}

case ${1:-install} in
  install) install_all ;;
  secret) import_secret "${2:-}" ;;
  session-key) new_session_key ;;
  check) check ;;
  *) die "usage: setup.sh [install | secret NAME | session-key | check]" ;;
esac
