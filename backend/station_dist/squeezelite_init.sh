#!/bin/sh
# Squeezelite autostart hook for Porfiriy & Music Assistant
case "${1:-start}" in
    start)
        # Run start in background so boot rcS is not blocked
        sh /data/opt/squeezelite/service start >/dev/null 2>&1 &
        ;;
    stop)
        sh /data/opt/squeezelite/service stop
        ;;
    restart)
        sh /data/opt/squeezelite/service restart
        ;;
    status)
        exec sh /data/opt/squeezelite/service status
        ;;
    *)
        exec sh /data/opt/squeezelite/service "$@"
        ;;
esac
