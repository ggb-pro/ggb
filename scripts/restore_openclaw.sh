#!/bin/bash
# OpenClaw restore script
# Usage: sudo bash restore_openclaw.sh
# Restores from /home/ubuntu/openclaw-backup.tar.gz and openclaw-config-backup.tar.gz

set -e

BACKUP_DIR="/home/ubuntu"
MAIN_BACKUP="$BACKUP_DIR/openclaw-backup.tar.gz"
CONFIG_BACKUP="$BACKUP_DIR/openclaw-config-backup.tar.gz"

if [ ! -f "$MAIN_BACKUP" ]; then
    echo "ERROR: $MAIN_BACKUP not found"
    exit 1
fi

echo "=== Restoring OpenClaw data ==="
tar xzf "$MAIN_BACKUP" -C /root/
echo "Restored .openclaw/ and .cache/ms-playwright/"

if [ -f "$CONFIG_BACKUP" ]; then
    echo "=== Restoring config files ==="
    tar xzf "$CONFIG_BACKUP" -C /
    echo "Restored config files"
fi

echo "=== Setting permissions ==="
chown -R root:root /root/.openclaw /root/.cache/ms-playwright

echo "=== Reloading systemd ==="
systemctl daemon-reload

echo ""
echo "Restore complete. To start OpenClaw:"
echo "  1. Start the gateway process manually or via its original startup method"
echo "  2. Enable the upgrade guard: sudo systemctl enable openclaw-upgrade-guard"
echo ""
echo "Disk usage:"
du -sh /root/.openclaw /root/.cache/ms-playwright
