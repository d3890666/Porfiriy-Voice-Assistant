#!/bin/sh
# porfiriy_set_volume_v2
# Quasar set_volume.sh wrapper with Porfiriy Auto-Ducking coordination & Music Assistant sync
target="${1:-50}"
case "$target" in
    *[!0-9]*|"") target=50 ;;
esac

echo "$target" > /tmp/quasar_volume

active_flag="/tmp/station_ducking_active"
saved_vol_file="/tmp/station_ducking_saved_vol"
vol_changed_flag="/tmp/station_ducking_vol_changed"
duck_pidfile="/data/opt/station-ducking/ducking.pid"

is_bad_host() {
    case "$1" in
        ""|127.*|172.17.*|172.30.*|169.254.*|0.0.0.0|localhost) return 0 ;;
        *) return 1 ;;
    esac
}

resolve_porfiriy_host() {
    h=$(cat /data/opt/porfiriy_host 2>/dev/null | tr -d ' \r\n' || echo "")
    if ! is_bad_host "$h"; then
        printf '%s' "$h"
        return 0
    fi
    if [ -f /data/opt/quasar-bridge/config.json ]; then
        h=$(awk -F'://' '/http:\/\//{split($2,a,"[:/\" ]"); print a[1]; exit}' /data/opt/quasar-bridge/config.json 2>/dev/null || echo "")
        if ! is_bad_host "$h"; then
            printf '%s' "$h"
            return 0
        fi
    fi
    if [ -f /data/opt/squeezelite/squeezelite.env ]; then
        h=$(grep '^SQUEEZELITE_SERVER=' /data/opt/squeezelite/squeezelite.env 2>/dev/null | head -n 1 | cut -d= -f2- | tr -d '"'\'' \r' | cut -d: -f1 || echo "")
        if ! is_bad_host "$h"; then
            printf '%s' "$h"
            return 0
        fi
    fi
    printf '%s' "192.168.1.50"
}

sync_volume_to_ha() {
    vol_val="$1"
    porfiriy_host=$(resolve_porfiriy_host)
    porfiriy_port=$(cat /data/opt/porfiriy_port 2>/dev/null | tr -d ' \r\n' || echo "8000")
    case "$porfiriy_port" in *[!0-9]*|"") porfiriy_port="8000" ;; esac

    player_name=""
    if [ -f /data/opt/squeezelite/squeezelite.env ]; then
        player_name=$(grep '^SQUEEZELITE_NAME=' /data/opt/squeezelite/squeezelite.env 2>/dev/null | head -n 1 | cut -d= -f2- | tr -d '"'\''\r' || echo "")
    fi
    [ -z "$player_name" ] && [ -f /data/opt/station-airplay/name ] && player_name=$(cat /data/opt/station-airplay/name 2>/dev/null | tr -d '\r\n' || echo "")
    player_name_esc=$(printf '%s' "$player_name" | sed 's/\\/\\\\/g; s/"/\\"/g')

    curl -s -m 2 -X POST "http://${porfiriy_host}:${porfiriy_port}/api/station/volume_sync" \
        -H "Content-Type: application/json" \
        -d "{\"volume\": $vol_val, \"player_name\": \"$player_name_esc\"}" >/dev/null 2>&1 &
}

ducking_is_active=0
if [ -f "$active_flag" ]; then
    dpid=$(cat "$duck_pidfile" 2>/dev/null || echo "")
    if [ -n "$dpid" ] && kill -0 "$dpid" 2>/dev/null; then
        ducking_is_active=1
    else
        rm -f "$active_flag" "$saved_vol_file" "$vol_changed_flag"
    fi
fi

if [ "$ducking_is_active" -eq 1 ]; then
    # When ducking is active, queue restoration volume and let ducking_service sync on unduck
    echo "$target" > "$saved_vol_file"
    touch "$vol_changed_flag"
    echo "$(date '+%Y/%m/%d %H:%M:%S') [SET_VOL] Ducking active: queued restoration to ${target}%" >> /data/opt/station-ducking/ducking.log 2>/dev/null || true
else
    amixer set 'Master' "${target}%" >/dev/null 2>&1 || true
    echo "$(date '+%Y/%m/%d %H:%M:%S') [SET_VOL] Master set to ${target}%" >> /data/opt/station-ducking/ducking.log 2>/dev/null || true
    sync_volume_to_ha "$target"
fi
