#!/system/bin/sh
ROOT=/data/adb/device_services
[ -e "$ROOT/disabled" ] && exit 0
export PYTHONPATH=/data/adb
export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib
PY=/data/data/com.termux/files/usr/bin/python3
umask 077
while [ "$(getprop sys.boot_completed)" != 1 ]; do sleep 5; done
while [ ! -x "$PY" ]; do sleep 10; done
"$PY" -m device_services.daemon keeper </dev/null >/dev/null 2>&1 &
