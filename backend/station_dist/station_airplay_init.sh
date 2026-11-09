#!/bin/sh
# Station AirPlay autostart hook for Porfiriy & Music Assistant
case "${1:-start}" in
    start)
        sh /data/opt/station-airplay/service start >/dev/null 2>&1 &
        ;;
    stop)
        sh /data/opt/station-airplay/service stop
        ;;
    restart)
        sh /data/opt/station-airplay/service restart
        ;;
    status)
        exec sh /data/opt/station-airplay/service status
        ;;
    *)
        exec sh /data/opt/station-airplay/service "$@"
        ;;
esac
