#!/bin/bash
# Weekly Outpost disk cleanup. Safe to run anytime; only removes:
#  1. ~/cloud-agents-backup-* dirs beyond the 2 newest (legacy Mac path)
#  2. DANGLING container images (tagged images untouched; never --all)
#  3. Stale agent build artifacts in /tmp older than 7 days
set -u

if command -v docker >/dev/null 2>&1; then
  CBIN="$(command -v docker)"
elif [ -x "$HOME/cloud-agents/rt/bin/container" ]; then
  CBIN="$HOME/cloud-agents/rt/bin/container"
else
  CBIN=""
fi
DATA_VOL="/"
if [ -d /System/Volumes/Data ]; then
  DATA_VOL="/System/Volumes/Data"
fi

avail_before=$(df -k "$DATA_VOL" 2>/dev/null | tail -1 | awk '{print $4}')

# 1. Old backups — keep the 2 newest
ls -dt "$HOME"/cloud-agents-backup-* 2>/dev/null | tail -n +3 | while IFS= read -r d; do
  [ -n "$d" ] && rm -rf "$d"
done

# 2. Dangling container images only
if [ -n "$CBIN" ] && [ -x "$CBIN" ]; then
  if [ "$(basename "$CBIN")" = "docker" ]; then
    "$CBIN" image prune -f < /dev/null >/dev/null 2>&1 || true
  else
    "$CBIN" image prune < /dev/null >/dev/null 2>&1 || true
  fi
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
