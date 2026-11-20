#!/bin/sh
set -eu
base="/data/opt/squeezelite"
binary="$base/squeezelite"
pidfile="$base/squeezelite.pid"
logfile="$base/squeezelite.log"
envfile="$base/squeezelite.env"

[ -f "$envfile" ] && . "$envfile"

def_server=$(cat /data/opt/porfiriy_host 2>/dev/null | tr -d ' \r\n' || echo "")
server="${SQUEEZELITE_SERVER:-$def_server}"
def_output="default"
if grep -q "pcm.music_duck" /etc/asound.conf 2>/dev/null; then
    def_output="music_duck"
fi
output="${SQUEEZELITE_OUTPUT:-$def_output}"
name="${SQUEEZELITE_NAME:-Яндекс Станция}"
buffers="${SQUEEZELITE_BUFFERS:-2048:3445}"
close_timeout="${SQUEEZELITE_CLOSE_TIMEOUT:-2}"
mixer_dev="${SQUEEZELITE_MIXER_DEVICE:-default}"
volume_ctl="${SQUEEZELITE_VOLUME_CONTROL:-Master}"

running() {
    test -s "$pidfile" || return 1
    pid=$(cat "$pidfile" 2>/dev/null) || return 1
    [ -n "$pid" ] || { rm -f "$pidfile"; return 1; }
    case "$pid" in *[!0-9]*) rm -f "$pidfile"; return 1 ;; esac
    test "$pid" -gt 1 || { rm -f "$pidfile"; return 1; }
    if [ -d "/proc/$pid" ]; then
        if grep -q "squeezelite" "/proc/$pid/cmdline" 2>/dev/null; then
            kill -0 "$pid" 2>/dev/null && return 0
        fi
    fi
    rm -f "$pidfile"
    return 1
}

wait_network() {
    i=0
    while [ "$i" -lt 20 ]; do
        if ip route show default 2>/dev/null | grep -q default; then
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

start() {
    if running; then
        echo "Squeezelite already running (PID $pid)"
        return 0
    fi
    rm -f "$pidfile"

    if [ ! -x "$binary" ]; then
        echo "Binary $binary not found or not executable" >&2
        return 1
    fi

    # Wait for network if starting at boot
    wait_network || true

    # Гарантируем регистрацию softvol Music микшера
    if [ "$output" = "music_duck" ]; then
        aplay -D music_duck -f S16_LE -c 2 -r 48000 -d 1 /dev/zero >/dev/null 2>&1 || true
        amixer set 'Music' 100% >/dev/null 2>&1 || true
    fi

    # Start squeezelite with hardware Master volume binding if configured
    vol_args=""
    if [ -n "$volume_ctl" ]; then
        vol_args="-O $mixer_dev -V $volume_ctl"
    fi

    nohup "$binary" -s "$server" -o "$output" $vol_args -a ":::0" -n "$name" -C "$close_timeout" -K -b "$buffers" >>"$logfile" 2>&1 &
    echo $! > "$pidfile"

    sleep 1
    if running; then
        echo "Squeezelite started (PID $(cat "$pidfile"))"
        return 0
    fi

    # Retry once after 2 seconds if first attempt failed (e.g. sound card busy during boot)
    sleep 2
    nohup "$binary" -s "$server" -o "$output" $vol_args -a ":::0" -n "$name" -C "$close_timeout" -K -b "$buffers" >>"$logfile" 2>&1 &
    echo $! > "$pidfile"

    sleep 1
    if running; then
        echo "Squeezelite started on retry (PID $(cat "$pidfile"))"
        return 0
    else
        echo "Squeezelite failed to start" >&2
        rm -f "$pidfile"
        [ -f "$logfile" ] && tail -n 10 "$logfile" >&2
        return 1
    fi
}

stop() {
    if running; then
        kill "$pid" 2>/dev/null || true
        i=0
        while running && [ "$i" -lt 10 ]; do
            sleep 1
            i=$((i + 1))
        done
        if running; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$pidfile"
        echo "Squeezelite stopped"
    else
        rm -f "$pidfile"
        echo "Squeezelite not running"
    fi
}

case "${1:-status}" in
    start) start ;;
    stop) stop ;;
    restart) stop; sleep 1; start ;;
    status)
        if running; then
            echo "Squeezelite running (PID $pid)"
            exit 0
        else
            echo "Squeezelite stopped"
            exit 1
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac

