#!/bin/sh
case "$1" in
    start)
        if [ -f /data/opt/disable_alice ] && [ -x /data/opt/porfiriy-stationd/service ]; then
            /data/opt/porfiriy-stationd/service start
        fi
        ;;
    stop)
        [ -x /data/opt/porfiriy-stationd/service ] && /data/opt/porfiriy-stationd/service stop
        ;;
    restart)
        [ -x /data/opt/porfiriy-stationd/service ] && /data/opt/porfiriy-stationd/service restart
        ;;
    *)
        echo "Usage: $0 {start|stop|restart}"
        exit 1
        ;;
esac
