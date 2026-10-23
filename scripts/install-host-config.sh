#!/bin/bash
set -euo pipefail

# Install C3PO's host configuration on the VM. Idempotent; run on the VM:
#   sudo bash ~/c3po/scripts/install-host-config.sh
#
# - systemd units: c3po.service, the daily c3po-refresh timer, and a weekly
#   reboot (exe.dev's kernel only updates at boot)
# - unattended-upgrades: Tailscale origin and automatic reboots at 08:00 UTC
# - apt's daily timers, which the exe.dev image ships masked (so
#   unattended-upgrades was configured but never ran)

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash $0" >&2
    exit 1
fi

repo="$(cd "$(dirname "$0")/.." && pwd)"

install -m 0644 "$repo/deploy/systemd/c3po.service" /etc/systemd/system/c3po.service
install -m 0644 "$repo/deploy/systemd/c3po-refresh.service" /etc/systemd/system/c3po-refresh.service
install -m 0644 "$repo/deploy/systemd/c3po-refresh.timer" /etc/systemd/system/c3po-refresh.timer
install -m 0644 "$repo/deploy/systemd/weekly-reboot.service" /etc/systemd/system/weekly-reboot.service
install -m 0644 "$repo/deploy/systemd/weekly-reboot.timer" /etc/systemd/system/weekly-reboot.timer
install -m 0644 "$repo/deploy/apt/52c3po-unattended-upgrades" /etc/apt/apt.conf.d/52c3po-unattended-upgrades

systemctl unmask apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service
systemctl daemon-reload
systemctl enable --now apt-daily.timer apt-daily-upgrade.timer c3po-refresh.timer weekly-reboot.timer
systemctl enable c3po.service

# Show the unattended-upgrades decision without changing anything.
unattended-upgrade --dry-run 2>&1 | tail -3 || true
systemctl list-timers apt-daily.timer apt-daily-upgrade.timer c3po-refresh.timer weekly-reboot.timer --no-pager
