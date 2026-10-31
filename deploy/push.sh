#!/usr/bin/env bash
# Ships the app from the laptop to the VM. Run from Git Bash at the repo root; HOST is the SSH destination,
# e.g. ubuntu@VM-NAME. The VM user needs passwordless sudo (the default on Oracle images). See docs/REMOTE.md.
#
#   deploy/push.sh HOST [--force]        build the UI, copy the app to /opt/ytps, install the units, restart;
#                                        refuses uncommitted changes unless --force
#   deploy/push.sh HOST refresh-token    move the YouTube refresh token from this laptop's keychain into the VM's
#                                        encrypted credential, over SSH stdin (never a file, never on screen)
#   deploy/push.sh HOST desktop-client   copy the Desktop OAuth client JSON to /etc/ytps/client_secret.json
set -euo pipefail

FORCE=0
ARGS=()
for arg in "$@"; do
  if [[ $arg == --force ]]; then FORCE=1; else ARGS+=("$arg"); fi
done
set -- "${ARGS[@]}"
HOST=${1:?usage: deploy/push.sh HOST [refresh-token | desktop-client] [--force]}
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
UV=(py -3.12 -m uv)
REMOTE_UV="UV_PROJECT_ENVIRONMENT=/opt/ytps-venv UV_PYTHON_INSTALL_DIR=/opt/ytps-python UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR=/var/cache/ytps-uv /usr/local/bin/uv"

die() {
  printf 'push.sh: %s\n' "$*" >&2
  exit 1
}

deploy() {
  # the working tree is what ships, so ship only what is committed
  if [[ -n $(git -C "$ROOT" status --porcelain) ]] && ((!FORCE)); then
    git -C "$ROOT" status --short >&2
    die "uncommitted changes (listed above); commit them or rerun with --force"
  fi
  (cd "$ROOT/web" && { [[ -d node_modules ]] || npm ci; } && npm run build)
  [[ -f $ROOT/app/web/static/index.html ]] || die "the UI build did not produce app/web/static/index.html"

  # The build output is git-ignored, so ship the working tree, minus anything local or secret.
  # Patterns starting with ./ match only at the repo root.
  tar -C "$ROOT" -czf - \
    --exclude='.git' --exclude='./data' --exclude='node_modules' --exclude='.venv' --exclude='__pycache__' \
    --exclude='.pytest_cache' --exclude='.env' --exclude='./graphs/mine.yaml' --exclude='./secrets' \
    --exclude='client_secret*.json' --exclude='credentials*.json' --exclude='token*.json' --exclude='*.db' \
    --exclude='*.sqlite' --exclude='dist' --exclude='./.[!.]*' \
    . |
    ssh "$HOST" "sudo -n bash -c '
      set -euo pipefail
      rm -rf /opt/ytps.new && install -d -m 0755 /opt/ytps.new
      tar -xzf - -C /opt/ytps.new --no-same-owner --no-same-permissions
      chmod -R u=rwX,go=rX /opt/ytps.new
      cd /opt/ytps.new && $REMOTE_UV sync --frozen --no-dev --compile-bytecode --python 3.12
      rm -rf /opt/ytps.old
      if [ -d /opt/ytps ]; then mv /opt/ytps /opt/ytps.old; fi
      mv /opt/ytps.new /opt/ytps
      install -m 0644 /opt/ytps/deploy/ytps.service /opt/ytps/deploy/ytps-run.service /opt/ytps/deploy/ytps-run.timer /etc/systemd/system/
      systemctl daemon-reload
      systemctl try-restart ytps.service
    '"
  printf 'Deployed to %s. A stopped service stays stopped; start it with: sudo systemctl enable --now ytps\n' "$HOST"
}

refresh_token() {
  # prints the token only into the pipe; the VM reads it from stdin and encrypts it
  (cd "$ROOT" && "${UV[@]}" run --no-sync python -c '
import sys
from app import auth, secretstore
token = secretstore.get(auth.REFRESH_TOKEN)
if not token:
    sys.exit("No refresh token in the keychain on this laptop. Run the auth command first.")
sys.stdout.write(token)
') | ssh "$HOST" "sudo -n bash /opt/ytps/deploy/setup.sh secret refresh-token"
}

desktop_client() {
  local path=${CLIENT_SECRET_PATH:-$HOME/.config/youtube-playlist-sorter/client_secret.json}
  [[ -f $path ]] || die "no Desktop client JSON at $path (set CLIENT_SECRET_PATH)"
  ssh "$HOST" "sudo -n sh -c 'umask 027 && cat > /etc/ytps/client_secret.json.new && chown root:ytps /etc/ytps/client_secret.json.new && mv -f /etc/ytps/client_secret.json.new /etc/ytps/client_secret.json'" <"$path"
  printf 'Copied the Desktop client JSON to %s:/etc/ytps/client_secret.json\n' "$HOST"
}

case ${2:-deploy} in
  deploy) deploy ;;
  refresh-token) refresh_token ;;
  desktop-client) desktop_client ;;
  *) die "usage: deploy/push.sh HOST [refresh-token | desktop-client]" ;;
esac
