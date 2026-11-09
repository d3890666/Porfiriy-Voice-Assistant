#!/bin/sh
# Quasar set_volume.sh wrapper with Porfiriy Auto-Ducking coordination
target="${1:-50}"
case "$target" in
    *[!0-9]*) target=50 ;;
esac

echo "$target" > /tmp/quasar_volume

if [ -f /tmp/station_ducking_active ]; then
    # When ducking is active, update target restoration volume
    echo "$target" > /tmp/station_ducking_saved_vol
    echo "$(date '+%Y/%m/%d %H:%M:%S') [SET_VOL] Ducking active: queued restoration to ${target}%" >> /data/opt/station-ducking/ducking.log 2>/dev/null || true
else
    amixer set 'Master' "${target}%" >/dev/null 2>&1 || true
    echo "$(date '+%Y/%m/%d %H:%M:%S') [SET_VOL] Master set to ${target}%" >> /data/opt/station-ducking/ducking.log 2>/dev/null || true
fi
