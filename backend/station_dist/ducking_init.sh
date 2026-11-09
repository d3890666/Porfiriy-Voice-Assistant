#!/bin/sh
# Station Ducking autostart hook for Porfiriy & Music Assistant
case "${1:-start}" in
    start)
        sh /data/opt/station-ducking/service start >/dev/null 2>&1 &
        ;;
    stop)
        sh /data/opt/station-ducking/service stop
        ;;
    restart)
        sh /data/opt/station-ducking/service restart
        ;;
    status)
        exec sh /data/opt/station-ducking/service status
        ;;
    *)
        exec sh /data/opt/station-ducking/service "$@"
        ;;
esac
