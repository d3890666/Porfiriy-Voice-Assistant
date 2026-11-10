#!/bin/sh
set -eu
base="/data/opt/station-ducking"
pidfile="$base/ducking.pid"
logfile="$base/ducking.log"
envfile="$base/ducking.env"

[ -f "$envfile" ] && . "$envfile"

airplay_log="${STATION_AIRPLAY_LOG:-/data/opt/station-airplay/receiver.log}"
duck_volume="${DUCK_VOLUME:-15}"
max_duck_duration="${MAX_DUCK_DURATION:-8}"
active_flag="/tmp/station_ducking_active"
saved_vol_file="/tmp/station_ducking_saved_vol"
vol_changed_flag="/tmp/station_ducking_vol_changed"

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

restore_from_ducking() {
    reason="$1"
    restore_vol=""
    if [ -f "$saved_vol_file" ]; then
        restore_vol=$(cat "$saved_vol_file" 2>/dev/null || echo "")
    fi
    case "$restore_vol" in *[!0-9]*|"") restore_vol=60 ;; esac

    should_sync=0
    [ -f "$vol_changed_flag" ] && should_sync=1

    amixer set 'Master' "${restore_vol}%" >/dev/null 2>&1 || true
    rm -f "$active_flag" "$saved_vol_file" "$vol_changed_flag"
    echo "$(date '+%Y/%m/%d %H:%M:%S') ${reason} volume restored to ${restore_vol}%" >> "$logfile"

    if [ "$should_sync" -eq 1 ]; then
        sync_volume_to_ha "$restore_vol"
    fi
}

running() {
    test -s "$pidfile" || return 1
    pid=$(cat "$pidfile" 2>/dev/null) || return 1
    [ -n "$pid" ] || { rm -f "$pidfile"; return 1; }
    case "$pid" in *[!0-9]*) rm -f "$pidfile"; return 1 ;; esac
    test "$pid" -gt 1 || { rm -f "$pidfile"; return 1; }
    if [ -d "/proc/$pid" ]; then
        if grep -q "station-ducking" "/proc/$pid/cmdline" 2>/dev/null || grep -q "sh" "/proc/$pid/cmdline" 2>/dev/null; then
            kill -0 "$pid" 2>/dev/null && return 0
        fi
    fi
    rm -f "$pidfile"
    return 1
}

run_watchdog() {
    mkdir -p "$(dirname "$airplay_log")"
    touch "$airplay_log"

    echo "$(date '+%Y/%m/%d %H:%M:%S') [DUCK] Station Ducking watchdog started (target_duck_vol=${duck_volume}%)" >> "$logfile"
    rm -f "$active_flag" "$saved_vol_file" "$vol_changed_flag"

    tail -n 0 -F "$airplay_log" 2>/dev/null | while IFS= read -r line; do
        now=$(date +%s)

        # Safety check: if ducked for longer than max_duck_duration, auto-restore
        if [ -f "$active_flag" ]; then
            mtime=$(stat -c %Y "$active_flag" 2>/dev/null || echo "$now")
            if [ $((now - mtime)) -gt "$max_duck_duration" ]; then
                restore_from_ducking "[WARN] Ducking timeout (${max_duck_duration}s) exceeded:"
            fi
        fi

        case "$line" in
            *alice_ipc_state*state=1*|*alice_ipc_state*state=3*|*alice_ipc_state*state=2*)
                if [ ! -f "$active_flag" ]; then
                    curr_vol=$(amixer sget Master 2>/dev/null | awk -F'[][]' '/%/ {print $2; exit}' | tr -d '%')
                    case "$curr_vol" in *[!0-9]*|"") curr_vol=60 ;; esac

                    if [ "$curr_vol" -gt "$duck_volume" ]; then
                        rm -f "$vol_changed_flag"
                        echo "$curr_vol" > "$saved_vol_file"
                        touch "$active_flag"
                        amixer set 'Master' "${duck_volume}%" >/dev/null 2>&1 || true
                        echo "$(date '+%Y/%m/%d %H:%M:%S') [DUCK] Alice invoked: ducking volume $curr_vol% -> ${duck_volume}%" >> "$logfile"
                    fi
                fi
                ;;
            *alice_ipc_state*state=0*)
                if [ -f "$active_flag" ]; then
                    restore_from_ducking "[UNDUCK] Alice idle:"
                fi
                ;;
        esac
    done
}

start() {
    if running; then
        echo "Station Ducking already running (PID $pid)"
        return 0
    fi
    rm -f "$pidfile"

    mkdir -p "$base"
    # Launch background watchdog process
    ( run_watchdog ) >>"$logfile" 2>&1 &
    echo $! > "$pidfile"

    sleep 1
    if running; then
        echo "Station Ducking started (PID $(cat "$pidfile"))"
        return 0
    else
        echo "Station Ducking failed to start" >&2
        rm -f "$pidfile"
        [ -f "$logfile" ] && tail -n 10 "$logfile" >&2
        return 1
    fi
}

stop() {
    if running; then
        kill "$pid" 2>/dev/null || true
        i=0
        while running && [ "$i" -lt 5 ]; do
            sleep 1
            i=$((i + 1))
        done
        if running; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$pidfile"
        # Unduck on stop if was active
        if [ -f "$active_flag" ]; then
            restore_from_ducking "[STOP] Service stopping:"
        fi
        echo "Station Ducking stopped"
    else
        rm -f "$pidfile" "$active_flag" "$saved_vol_file" "$vol_changed_flag"
        echo "Station Ducking not running"
    fi
}

case "${1:-status}" in
    start) start ;;
    stop) stop ;;
    restart) stop; sleep 1; start ;;
    status)
        if running; then
            echo "Station Ducking running (PID $pid)"
            exit 0
        else
            echo "Station Ducking stopped"
            exit 1
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
