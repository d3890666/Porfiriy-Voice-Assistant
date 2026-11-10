#!/bin/sh
set -eu
base="/data/opt/alice-watcher"
pidfile="$base/watcher.pid"
logfile="$base/watcher.log"

airplay_log="${STATION_AIRPLAY_LOG:-/data/opt/station-airplay/receiver.log}"

resolve_porfiriy_host() {
    h=$(cat /data/opt/porfiriy_host 2>/dev/null | tr -d ' \r\n' || echo "")
    case "$h" in
        ""|127.*|172.17.*|172.30.*)
            h=""
            [ -f /data/opt/squeezelite/squeezelite.env ] && h=$(grep '^SQUEEZELITE_SERVER=' /data/opt/squeezelite/squeezelite.env 2>/dev/null | head -n 1 | cut -d= -f2- | tr -d '"'\'' \r' | cut -d: -f1)
            ;;
    esac
    [ -z "$h" ] && h="192.168.1.50"
    printf '%s' "$h"
}

send_state() {
    st="$1"
    host=$(resolve_porfiriy_host)
    curl -s -m 1 -X POST "http://${host}:8000/api/station/alice_state" \
        -H "Content-Type: application/json" \
        -d "{\"state\": \"$st\"}" >/dev/null 2>&1 &
}

running() {
    test -s "$pidfile" || return 1
    pid=$(cat "$pidfile" 2>/dev/null) || return 1
    [ -n "$pid" ] || { rm -f "$pidfile"; return 1; }
    case "$pid" in *[!0-9]*) rm -f "$pidfile"; return 1 ;; esac
    test "$pid" -gt 1 || { rm -f "$pidfile"; return 1; }
    if [ -d "/proc/$pid" ]; then
        if grep -q "alice-watcher" "/proc/$pid/cmdline" 2>/dev/null || grep -q "sh" "/proc/$pid/cmdline" 2>/dev/null; then
            kill -0 "$pid" 2>/dev/null && return 0
        fi
    fi
    rm -f "$pidfile"
    return 1
}

run_watcher() {
    mkdir -p "$(dirname "$airplay_log")"
    touch "$airplay_log"
    echo "$(date '+%Y/%m/%d %H:%M:%S') [WATCHER] Alice state watcher started" >> "$logfile"
    in_dialog=0

    tail -n 0 -F "$airplay_log" 2>/dev/null | while IFS= read -r line; do
        case "$line" in
            *alice_ipc_state*state=1*)
                if [ "$in_dialog" -eq 0 ]; then
                    in_dialog=1
                    send_state "listening"
                    echo "$(date '+%Y/%m/%d %H:%M:%S') [WATCHER] Alice listening -> notified Porfiriy" >> "$logfile"
                fi
                ;;
            *alice_ipc_state*state=0*)
                if [ "$in_dialog" -eq 1 ]; then
                    in_dialog=0
                    send_state "idle"
                    echo "$(date '+%Y/%m/%d %H:%M:%S') [WATCHER] Alice idle -> notified Porfiriy" >> "$logfile"
                fi
                ;;
        esac
    done
}

start() {
    if running; then
        echo "Alice Watcher already running (PID $pid)"
        return 0
    fi
    rm -f "$pidfile"
    mkdir -p "$base"
    ( run_watcher ) >>"$logfile" 2>&1 &
    echo $! > "$pidfile"
    sleep 1
    if running; then
        echo "Alice Watcher started (PID $(cat "$pidfile"))"
        return 0
    fi
    echo "Alice Watcher failed to start" >&2
    rm -f "$pidfile"
    return 1
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
        echo "Alice Watcher stopped"
    else
        rm -f "$pidfile"
        echo "Alice Watcher not running"
    fi
}

case "${1:-status}" in
    start) start ;;
    stop) stop ;;
    restart) stop; sleep 1; start ;;
    status)
        if running; then
            echo "Alice Watcher running (PID $pid)"
            exit 0
        else
            echo "Alice Watcher stopped"
            exit 1
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
