#!/usr/bin/env bash
# One-time (re-runnable) server setup, run with sudo on oracle-kr-2.
# Usage: sudo deploy/server/setup.sh   (reads BUS_SYNC_RUNNER_TOKEN from stdin on first run)
set -euo pipefail
root=/srv/osm-korea-bus-sync
user=osmbus
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# Read the token before any installer can consume standard input.
token=""
if [[ ! -f /etc/osm-korea-bus-sync/runner.env ]]; then
  read -r token || true
  [[ -n "$token" ]] || { echo "runner token expected on standard input" >&2; exit 1; }
fi

id "$user" >/dev/null 2>&1 || useradd --system --home-dir "$root" --shell /usr/sbin/nologin "$user"
install -d -o "$user" -g "$user" -m 0750 "$root" "$root/releases" "$root/shared" "$root/shared/data" "$root/shared/reports" "$root/bin" "$root/.cache" "$root/.local"
install -o root -g "$user" -m 0750 "$here/pull-deploy" "$root/bin/pull-deploy"

if [[ ! -x "$root/.local/bin/uv" ]]; then
  sudo -u "$user" env HOME="$root" UV_INSTALL_DIR="$root/.local/bin" UV_NO_MODIFY_PATH=1 \
    sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' < /dev/null
fi

install -d -o root -g "$user" -m 0750 /etc/osm-korea-bus-sync
if [[ -n "$token" ]]; then
  umask 027
  printf 'BUS_SYNC_CONTROL_URL=https://osm-korea-bus-sync.lidarbtc.workers.dev\nBUS_SYNC_RUNNER_TOKEN=%s\n' "$token" > /etc/osm-korea-bus-sync/runner.env
  chown root:"$user" /etc/osm-korea-bus-sync/runner.env
  chmod 0640 /etc/osm-korea-bus-sync/runner.env
fi

install -m 0644 "$here"/systemd/*.service "$here"/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
sudo -u "$user" env HOME="$root" "$root/bin/pull-deploy"
systemctl enable --now osm-korea-bus-sync.timer osm-korea-bus-sync-control.timer osm-korea-bus-sync-deploy.timer
systemctl list-timers 'osm-korea-bus-sync*' --no-pager
