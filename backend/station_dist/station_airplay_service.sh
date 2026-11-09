#!/bin/sh
# Dedicated Station AirPlay service. No changes to vendor audio services.
set -eu
self=$0
while [ -L "$self" ]; do
 link=$(readlink "$self")
 case "$link" in /*) self=$link;; *) self=$(dirname "$self")/$link;; esac
done
base=$(CDPATH= cd "$(dirname "$self")" && pwd -P)
pidfile="$base/receiver.pid"
binary="$base/bin/station-airplay"

running() {
 [ -s "$pidfile" ] || return 1
 pid=$(cat "$pidfile" 2>/dev/null) || return 1
 [ -n "$pid" ] || { rm -f "$pidfile"; return 1; }
 case "$pid" in *[!0-9]*) rm -f "$pidfile"; return 1;; esac
 kill -0 "$pid" 2>/dev/null || { rm -f "$pidfile"; return 1; }
 target=$(readlink "/proc/$pid/exe" 2>/dev/null) || { rm -f "$pidfile"; return 1; }
 case "$target" in "$binary"|"$binary (deleted)") return 0;; *) rm -f "$pidfile"; return 1;; esac
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

case "${1:-status}" in
 start)
  if running; then echo "Station AirPlay already running ($pid)";exit 0;fi
  rm -f "$pidfile"
  wait_network || true
  name=$(cat "$base/name")
  iface=$(cat "$base/interface" 2>/dev/null || echo "")
  device=$(cat "$base/device" 2>/dev/null || echo "default")
  aplay=$(cat "$base/aplay" 2>/dev/null || echo "")
  # A failing library/config preflight must fail start before reporting a PID.
  "$binary" -check -name "$name" -interface "$iface" -device "$device" -aplay "$aplay" -data-dir "$base/state"
  nohup "$binary" -name "$name" -interface "$iface" -device "$device" -aplay "$aplay" -data-dir "$base/state" </dev/null >>"$base/receiver.log" 2>&1 &
  pid=$!;printf "%s\n" "$pid" >"$pidfile"
  sleep 1
  if ! running; then rm -f "$pidfile";tail -12 "$base/receiver.log" >&2;exit 1;fi
  echo "Station AirPlay running ($pid)"
  ;;
 stop)
  if running; then
   kill -TERM "$pid" 2>/dev/null || true
   count=0
   while running && [ "$count" -lt 5 ]; do sleep 1;count=$((count+1));done
   if running; then kill -KILL "$pid" 2>/dev/null || true;fi
  fi
  rm -f "$pidfile"
  echo "Station AirPlay stopped"
  ;;
 restart) "$self" stop; sleep 1; "$self" start;;
 status) if running;then echo "Station AirPlay running ($pid)";else echo "Station AirPlay stopped";exit 1;fi;;
 *) echo "Usage: service {start|stop|restart|status}" >&2;exit 2;;
esac
