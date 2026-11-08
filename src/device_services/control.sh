#!/system/bin/sh
ROOT=/data/adb/device_services
export PYTHONPATH=/data/adb
export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib
PY=/data/data/com.termux/files/usr/bin/python3
case "$1" in
  status) exec "$PY" -m device_services.daemon status ;;
  start) rm -f "$ROOT/disabled"; sh "$ROOT/boot.sh" ;;
  stop) touch "$ROOT/disabled" ;;
  display-on) rm -f "$ROOT/display.disabled" ;;
  display-off) touch "$ROOT/display.disabled" ;;
  campus-on) rm -f "$ROOT/campus.disabled" ;;
  campus-off) touch "$ROOT/campus.disabled" ;;
  *) echo 'Usage: control.sh status|start|stop|display-on|display-off|campus-on|campus-off'; exit 2 ;;
esac
