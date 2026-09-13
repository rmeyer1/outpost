#!/bin/bash
# Weekly outpost disk cleanup. Safe to run anytime; only removes:
#  1. ~/outpost-backup-* dirs beyond the 2 newest
#  2. DANGLING container images (tagged images untouched; never --all)
#  3. Stale agent build artifacts in /tmp older than 7 days
set -u

CBIN="$HOME/outpost/rt/bin/container"
DATA_VOL="/System/Volumes/Data"

avail_before=$(df -k "$DATA_VOL" 2>/dev/null | tail -1 | awk '{print $4}')

# 1. Old backups — keep the 2 newest
ls -dt "$HOME"/outpost-backup-* 2>/dev/null | tail -n +3 | while IFS= read -r d; do
  [ -n "$d" ] && rm -rf "$d"
done

# 2. Dangling container images only
if [ -x "$CBIN" ]; then
  "$CBIN" image prune < /dev/null >/dev/null 2>&1 || true
fi

# 3. Stale /tmp agent artifacts
find /tmp -maxdepth 1 \( -name "*.tar" -o -name "*.tar.gz" -o -name "*.tgz" \) -mtime +7 -delete 2>/dev/null || true
find /tmp -maxdepth 1 -name "ca-seed-*" -mtime +1 -delete 2>/dev/null || true

avail_after=$(df -k "$DATA_VOL" 2>/dev/null | tail -1 | awk '{print $4}')
if [ -n "$avail_before" ] && [ -n "$avail_after" ]; then
  freed_mb=$(( (avail_after - avail_before) / 1024 ))
  pct=$(df -h "$DATA_VOL" 2>/dev/null | tail -1 | awk '{print $5}')
  echo "cleanup done: freed ~${freed_mb} MB; Data volume ${pct} full"
else
  echo "cleanup done (disk reading unavailable)"
fi
