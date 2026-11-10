#!/bin/sh
# Alice Watcher autostart hook for Porfiriy server-side ducking
case "${1:-start}" in
    start)
        sh /data/opt/alice-watcher/service start >/dev/null 2>&1 &
        ;;
    stop)
        sh /data/opt/alice-watcher/service stop
        ;;
    restart)
        sh /data/opt/alice-watcher/service restart
        ;;
    status)
        exec sh /data/opt/alice-watcher/service status
        ;;
    *)
        exec sh /data/opt/alice-watcher/service "$@"
        ;;
esac
