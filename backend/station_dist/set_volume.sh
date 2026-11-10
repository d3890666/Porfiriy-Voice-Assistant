#!/bin/sh
# porfiriy_set_volume_v2
# Quasar set_volume.sh wrapper with Porfiriy Auto-Ducking coordination & Music Assistant sync
target="${1:-50}"
case "$target" in
    *[!0-9]*|"") target=50 ;;
esac

echo "$target" > /tmp/quasar_volume

porfiriy_host=$(cat /data/opt/porfiriy_host 2>/dev/null | tr -d ' \r\n' || echo "")
case "$porfiriy_host" in
    ""|127.*|172.17.*|172.30.*)
        porfiriy_host=""
        [ -f /data/opt/squeezelite/squeezelite.env ] && porfiriy_host=$(grep '^SQUEEZELITE_SERVER=' /data/opt/squeezelite/squeezelite.env 2>/dev/null | head -n 1 | cut -d= -f2- | tr -d '"'\'' \r' | cut -d: -f1)
        ;;
esac
[ -z "$porfiriy_host" ] && porfiriy_host="192.168.1.50"

if [ -f /tmp/station_ducking_active ]; then
    # When ducking is active, queue restoration volume and let ducking_service sync on unduck
    echo "$target" > /tmp/station_ducking_saved_vol
    touch /tmp/station_ducking_vol_changed
    echo "$(date '+%Y/%m/%d %H:%M:%S') [SET_VOL] Ducking active: queued restoration to ${target}%" >> /data/opt/station-ducking/ducking.log 2>/dev/null || true
else
    amixer set 'Master' "${target}%" >/dev/null 2>&1 || true
    echo "$(date '+%Y/%m/%d %H:%M:%S') [SET_VOL] Master set to ${target}%" >> /data/opt/station-ducking/ducking.log 2>/dev/null || true
    curl -s -m 2 -X POST "http://${porfiriy_host}:8000/api/station/volume_sync" \
        -H "Content-Type: application/json" \
        -d "{\"volume\": $target}" >/dev/null 2>&1 &
fi
