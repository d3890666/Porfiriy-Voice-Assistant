#!/bin/sh
set -eu
base="/data/opt/porfiriy-stationd"
pidfile="$base/stationd.pid"
logfile="$base/stationd.log"

resolve_porfiriy_host() {
    h=$(cat /data/opt/porfiriy_host 2>/dev/null | tr -d ' \r\n' || echo "")
    case "$h" in
        ""|127.*|172.17.*|172.30.*)
            h=""
            [ -f /data/opt/squeezelite/squeezelite.env ] && h=$(grep '^SQUEEZELITE_SERVER=' /data/opt/squeezelite/squeezelite.env 2>/dev/null | head -n 1 | cut -d= -f2- | tr -d '"'\'' \r' | cut -d: -f1)
            ;;
    esac
    printf '%s' "$h"
}

running() {
    test -s "$pidfile" || return 1
    pid=$(cat "$pidfile" 2>/dev/null) || return 1
    [ -n "$pid" ] || { rm -f "$pidfile"; return 1; }
    case "$pid" in *[!0-9]*) rm -f "$pidfile"; return 1 ;; esac
    if [ -d "/proc/$pid" ] && kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    rm -f "$pidfile"
    return 1
}

start() {
    if running; then
        echo "porfiriy-stationd already running (PID $pid)"
        return 0
    fi
    if [ ! -x "$base/lib64/ld-linux-aarch64.so.1" ] || [ ! -x "$base/python/bin/python3.11" ]; then
        echo "porfiriy-stationd runtime not installed in $base" >&2
        return 1
    fi
    if [ -f /data/opt/disable_alice ]; then
        killall quasar_launcher2 maind quasar-bridge 2>/dev/null || true
    fi
    host=$(resolve_porfiriy_host)
    export PORFIRIY_HOST="$host"
    export PORFIRIY_WS_PORT="8765"
    export PORFIRIY_MODELS_DIR="$base/models"
    export PYTHONPATH="$base/site"
    amixer set 'Music' 100% >/dev/null 2>&1 || true
    rm -f "$pidfile"
    (
        exec nice -n 5 "$base/lib64/ld-linux-aarch64.so.1" \
            --library-path "$base/lib64:$base/python/lib" \
            "$base/python/bin/python3.11" -u "$base/porfiriy_stationd.py"
    ) >>"$logfile" 2>&1 &
    echo $! > "$pidfile"
    sleep 1
    if running; then
        echo "porfiriy-stationd started (PID $(cat "$pidfile"))"
        return 0
    fi
    echo "porfiriy-stationd failed to start" >&2
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
        amixer set 'Music' 100% >/dev/null 2>&1 || true
        echo "porfiriy-stationd stopped"
    else
        rm -f "$pidfile"
        echo "porfiriy-stationd not running"
    fi
}

case "${1:-status}" in
    start) start ;;
    stop) stop ;;
    restart) stop; sleep 1; start ;;
    status)
        if running; then
            echo "porfiriy-stationd running (PID $pid)"
            exit 0
        else
            echo "porfiriy-stationd stopped"
            exit 1
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
