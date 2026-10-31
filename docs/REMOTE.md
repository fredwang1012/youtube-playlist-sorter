# Running on the Oracle VM

The sorter runs on an Oracle Cloud Always Free Linux VM, so it works with the laptop off. One systemd service
(`ytps`) serves the web UI and API on `127.0.0.1:8765`. A systemd timer (`ytps-run.timer`) runs the sort every 6
hours. `tailscale serve` publishes the page as HTTPS on the tailnet. The VM opens no inbound ports.

```
phone or laptop --(tailnet, HTTPS)--> tailscale serve on the VM --> 127.0.0.1:8765 (ytps.service)
                                                                    ytps-run.timer --> ytps-run.service
```

Files on the VM:

| Path | What | Owner, mode |
| --- | --- | --- |
| `/opt/ytps` | the app, copied by `deploy/push.sh` | root, read-only to the service |
| `/opt/ytps-venv`, `/opt/ytps-python` | Python 3.12 and the dependencies (uv) | root |
| `/var/lib/ytps` | `state.db`, the rule graph `mine.yaml`, run lock | `ytps`, 0700 |
| `/etc/ytps/env` | settings, no secrets | root:ytps, 0640 |
| `/etc/ytps/client_secret.json` | the Desktop OAuth client the YouTube token belongs to | root:ytps, 0640 |
| `/etc/ytps/creds/*.cred` | the three secrets, encrypted with `systemd-creds` | root, 0600 |

## Security model: read this first

- **The tailnet is shared.** It may also hold other people's accounts and devices. Being on the tailnet proves
  nothing. Google sign-in (default) admits only the owner's Google account. The Tailscale fallback admits only the
  owner's Tailscale login, never tailnet membership.
- **Use `tailscale serve` only, never Funnel.** Funnel would put the page on the public internet.
- **Keep the server bound to loopback.** `serve` binds to `127.0.0.1` in code. Do not change that, and do not
  open port 8765 in the Oracle security list or the VM firewall.
- **Treat the VM as single-tenant.** In Tailscale mode the server trusts the `Tailscale-User-Login` header that
  `tailscale serve` adds. Any process or user on the VM that can reach `127.0.0.1:8765` directly can send that header
  with any value and be the owner. Run nothing else on this VM that you would not trust with the owner's YouTube
  account, and give nobody else a login on it. Google mode does not trust the header at all.
- **Secrets are encrypted at rest.** The YouTube refresh token, the Google login client secret and the
  session-cookie key are `systemd-creds` files under `/etc/ytps/creds`. systemd decrypts them only for the two
  services. No secret is ever a plain file on the VM, an argument on a command line, or printed.
- **`PUBLIC_URL` must be exact.** Every write (tick, save, Check now, sign-out) is refused with 403 unless the
  browser's `Origin` header equals `PUBLIC_URL`. Google sign-in also builds its redirect address from it. Set it to
  the address the browser shows, for example `https://VM-NAME.tail1234.ts.net`, with no port and no trailing
  slash.

### Optional: limit the VM's port 443 to the owner's devices

Defence in depth, applied by the owner in the Tailscale admin console (Access controls). It changes what other users'
devices can reach too, so check his access still works after saving. When a policy has rules, Tailscale denies
everything they do not allow, so the rules below must also cover everything other users use today. Replace the
placeholders with the real logins (Users page of the admin console).

```jsonc
{
  "tagOwners": { "tag:ytps": ["<owner-login>"] },
  "acls": [
    // the owner reaches everything, including the sorter
    { "action": "accept", "src": ["<owner-login>"], "dst": ["*:*"] },
    // other users reach their own devices; add any shared device they use, but never tag:ytps
    { "action": "accept", "src": ["<other-user-login>"], "dst": ["autogroup:self:*"] }
  ]
}
```

Then tag the VM `tag:ytps` (Machines, the VM, Edit ACL tags). Identity headers still describe the device that sends
the request, so Tailscale mode keeps working on a tagged VM.

## Deploying

Everything below can be redone alone. Commands marked **laptop** run in Git Bash at the repo root. Commands marked
**VM** run in an SSH session on the VM. `HOST` is the SSH destination, for example `ubuntu@VM-NAME`.

Before you start:

- Upgrade the Oracle account to **Pay As You Go**. Always Free resources stay free, and a PAYG account's idle Always
  Free VM is not reclaimed. In Billing & Cost Management, Budgets, create a budget of **$1** with an alert to your
  email at 100%, so any accidental charge is noticed at once.
- The VM is on the tailnet (`tailscale status` on the VM shows it) and you can SSH to it. The VM user needs
  passwordless `sudo`, which is the default for `ubuntu` and `opc` on Oracle images.

### 1. Survey the VM (read-only)

**VM:**

```bash
uname -m                              # aarch64 (Ampere) or x86_64
cat /etc/os-release                   # distro and version
systemctl --version | head -1         # needs systemd 250 or newer (LoadCredentialEncrypted)
free -h; df -h / /var /opt            # the service needs about 150 MB of RAM and 1 GB of disk
systemctl list-units --type=service --state=running
ss -tlnp                              # nothing may already listen on 127.0.0.1:8765
tailscale version                     # 1.52 or newer for the serve syntax below
tailscale serve status                # what this VM already publishes
```

If systemd is older than 250 (Ubuntu 22.04 has 249), stop: the encrypted credentials need a newer distro.

### 2. Run setup.sh

**Laptop:** `scp deploy/setup.sh HOST:`

**VM:** `sudo bash setup.sh`

It creates the system user `ytps`, the directories in the table above, installs uv (pinned in the script) and
Python 3.12, creates the `systemd-creds` host key, and writes the `/etc/ytps/env` template. Running it again is
safe and keeps an existing env file.

### 3. Push the app

**Laptop:**

```bash
deploy/push.sh HOST
deploy/push.sh HOST desktop-client
```

The first command builds the UI (`npm run build`), copies the working tree to `/opt/ytps` without `.git`, `data`,
`node_modules`, `.env`, `graphs/mine.yaml` or any client or token JSON, installs the locked dependencies, installs the
three unit files and restarts the service if it is running. Run it again after every change; that is the whole
redeploy.

The second copies the **Desktop** OAuth client JSON (the one `auth` used on the laptop, at
`~/.config/youtube-playlist-sorter/client_secret.json`) to `/etc/ytps/client_secret.json`. The YouTube refresh token
only works together with this client. Google treats a Desktop client's secret as not confidential, so it is a
readable file rather than a credential.

If `uv sync --frozen` on the VM complains about the lock file, rerun setup with the laptop's uv version:
`sudo UV_VERSION=<version from 'py -3.12 -m uv --version'> bash /opt/ytps/deploy/setup.sh`.

### 4. Import the secrets

Each secret goes from a hidden prompt (or a pipe) straight into `systemd-creds encrypt` on stdin.

**YouTube refresh token**, moved from the laptop keychain. **Laptop:**

```bash
deploy/push.sh HOST refresh-token
```

This reads the token from the Windows keychain and pipes it over SSH into
`setup.sh secret refresh-token` on the VM. It is never shown, never a file and never an argument. If the laptop has
no token yet, run `py -3.12 -m uv run python -m app.cli auth` first. The laptop keeps its copy for dry runs; real
runs happen only on the VM.

**Session key**, generated on the VM and never shown. **VM:**

```bash
sudo bash /opt/ytps/deploy/setup.sh session-key
```

**Google login client secret**: after step 6. **VM** (`ssh -t HOST`, so the prompt can hide your typing):

```bash
sudo bash /opt/ytps/deploy/setup.sh secret login-client-secret
```

Paste the secret at the prompt and press Enter. Nothing is echoed.

### 5. Publish on the tailnet

Check the syntax for the installed version first: `tailscale serve --help`. Then **VM:**

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8765
tailscale serve status
```

`status` prints the address, for example `https://VM-NAME.tail1234.ts.net`. That is `PUBLIC_URL`. The
setting survives reboots. Never use `tailscale funnel`.

If the tailnet has HTTPS certificates turned off, the command says so: turn on HTTPS in the admin console (DNS page)
and rerun it.

### 6. Create the Google Web client

In the Google Cloud console, open the dedicated **`playlist-sorter`** project, the one that already holds the
Desktop client and the published consent screen. Never use the Talkback project or any other app's project: the
consent screen is shared by every client in a project.

1. Google Auth Platform, Clients, Create client, application type **Web application**, name it `ytps web`.
2. Authorized JavaScript origins: leave empty.
3. Authorized redirect URIs: `https://<vm>.<tailnet>.ts.net/auth/callback`, which is `PUBLIC_URL` plus
   `/auth/callback`.
4. Create. Copy the **Client ID** (for step 7) and the **Client secret** (for step 4, `login-client-secret`).
5. If Google asks for it, add `<tailnet>.ts.net` under Branding, Authorized domains.

Sign-in asks only for `openid email`; the YouTube permission stays with the Desktop client.

If Google refuses the `ts.net` redirect URI, use Tailscale mode instead (see "Tailscale mode" below).

### 7. Fill in the env file

**VM:** `sudo nano /etc/ytps/env` and set:

```
PUBLIC_URL=https://<vm>.<tailnet>.ts.net
AUTH_MODE=google
LOGIN_CLIENT_ID=<client id from step 6>
```

`OWNER_EMAIL` defaults to the owner's address. Then `sudo bash /opt/ytps/deploy/setup.sh check`: every line must say
`ok` before you go on.

### 8. Start the services

**VM:**

```bash
sudo systemctl enable --now ytps.service ytps-run.timer
systemctl status ytps --no-pager
systemctl list-timers ytps-run.timer
```

### 9. Check it

1. **VM:** `curl -s http://127.0.0.1:8765/healthz` prints `{"ok":true}`.
2. **Owner's laptop or phone:** `https://<vm>.<tailnet>.ts.net/healthz` prints `{"ok":true}` over the tailnet.
3. **Owner's phone:** open the address, sign in with the owner's Google account, and land on the Queue tab.
4. **Forged header from a non-owner device.** From another user's tailnet device (or any tailnet device not
   logged in as the owner), with the owner's real login in place of `<owner-login>`:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -H 'Tailscale-User-Login: <owner-login>' \
     https://<vm>.<tailnet>.ts.net/api/me
   ```

   Expect **403** in Tailscale mode, because `tailscale serve` replaces the forged header with that device's real
   login. Google mode ignores the header and answers **401**. A **200** means the header got through: stop the
   service (`sudo systemctl stop ytps`) and find out why before going on.
5. **Owner's phone:** pick the queue playlist (Queue tab, type its name to confirm). Press **Check now** and wait
   for the result. Add a test video to the queue playlist by hand in the YouTube app, refresh, tick it, and check it
   is gone from the playlist in YouTube.
6. **VM:** `journalctl -u ytps -u ytps-run --since today` shows no errors.

### Tailscale mode

The fallback when Google sign-in cannot be used. It trusts the identity `tailscale serve` attaches to each request,
so the single-tenant rule above matters more. Find the owner's login on the Users page of the admin console (for
example `name@github` or `name@gmail.com`), then in `/etc/ytps/env`:

```
AUTH_MODE=tailscale
OWNER_TAILSCALE_LOGIN=<owner-login>
```

`PUBLIC_URL` stays required. The units load all three credentials and refuse to start if a file is missing, so
without a Web client store a placeholder: `sudo bash /opt/ytps/deploy/setup.sh secret login-client-secret` and type
`unused`. Restart with `sudo systemctl restart ytps`. The Account section of the Activity tab
then says sign-out doesn't apply. Requests from tagged devices carry no identity and are refused.

## Running it

- **Logs:** `journalctl -u ytps -u ytps-run -f`. The server logs no request lines (callback URLs carry sign-in
  codes) and errors only by type.
- **Rules:** edit them in the Rules tab. They are saved to `/var/lib/ytps/mine.yaml`, the file the timer runs. To
  start from the laptop's graph once:
  `ssh HOST "sudo -n -u ytps tee /var/lib/ytps/mine.yaml >/dev/null" < graphs/mine.yaml`.
- **Schedule:** every 6 hours with up to 10 minutes of random delay, plus one catch-up run after downtime. To change
  it: `sudo systemctl edit ytps-run.timer` and override `OnCalendar=`.
- **Run now from the VM:** `sudo systemctl start ytps-run.service`.
- **Settings change:** edit `/etc/ytps/env`, then `sudo systemctl restart ytps`.
- **Redeploy:** `deploy/push.sh HOST` from the laptop.
- **YouTube access expired or revoked:** run `auth` on the laptop, then `deploy/push.sh HOST refresh-token` and
  `sudo systemctl restart ytps`.
- **Sign out everywhere:** the Account section at the bottom of the Activity tab. A new session key also signs out
  every device: `sudo bash /opt/ytps/deploy/setup.sh session-key && sudo systemctl restart ytps`.
- **If the service will not start** with `status=31/SYS` or a memory-mapping error in the journal, a library needs
  writable and executable memory. Run `sudo systemctl edit ytps ytps-run`, add `[Service]` and
  `MemoryDenyWriteExecute=no`, and report it.

## Revoking everything

Do these in order; each step stands alone if you only need part of it.

1. **Stop the app.** VM: `sudo systemctl disable --now ytps.service ytps-run.timer`.
2. **Unpublish.** VM: `sudo tailscale serve --https=443 off` (or `sudo tailscale serve reset` to clear every serve
   setting), then `tailscale serve status` shows nothing for it.
3. **Revoke YouTube access.** At https://myaccount.google.com/permissions, remove the playlist-sorter app. This kills
   the refresh token on the VM and the laptop alike.
4. **Delete the Web client.** Google Cloud console, `playlist-sorter` project, Google Auth Platform, Clients, delete
   `ytps web`. Its client secret stops working at once.
5. **Delete the secrets on the VM.** `sudo rm -f /etc/ytps/creds/*.cred /etc/ytps/client_secret.json`. Removing the
   VM's host key as well (`sudo rm /var/lib/systemd/credential.secret`) makes any copy of the `.cred` files
   unreadable, but also breaks every other credential encrypted on this VM.
6. **Remove the rest (optional).**

   ```bash
   sudo rm -f /etc/systemd/system/ytps.service /etc/systemd/system/ytps-run.service /etc/systemd/system/ytps-run.timer
   sudo systemctl daemon-reload
   sudo rm -rf /opt/ytps /opt/ytps.old /opt/ytps-venv /opt/ytps-python /var/lib/ytps /etc/ytps /var/cache/ytps-uv
   sudo userdel ytps
   ```

7. **Tailnet.** If you added the optional ACL, remove its rules and the `tag:ytps` tag.
