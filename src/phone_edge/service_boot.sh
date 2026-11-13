#!/system/bin/sh
set -eu

echo -1000 > /proc/$$/oom_score_adj || true
sysctl -w fs.inotify.max_user_watches=131072 || true
settings put secure accessibility_enabled 0 || true
settings put secure enabled_accessibility_services "" || true

backlight_off() {
    echo 0 > /sys/class/backlight/panel0-backlight/brightness
}

backlight_on() {
    echo 200 > /sys/class/backlight/panel0-backlight/brightness
}

EDGE_ROOT=/data/adb/maibot_edge
mkdir -p "$EDGE_ROOT/tailscale" "$EDGE_ROOT/state" "$EDGE_ROOT/run"

# 正式运行不依赖 ADB；仅显式配置 MAIBOT_ENABLE_ADB=1 时维护恢复用 ADB。
# Tailscale 状态路径沿用实机 state/run，不能另建空 state 触发重新登录。
ts_check_tick=0
ts_fail_count=0
while :; do
    if [ "${MAIBOT_ENABLE_ADB:-0}" = 1 ] && [ "$(getprop service.adb.tcp.port)" != 5555 ]; then
        setprop service.adb.tcp.port 5555
        stop adbd
        start adbd
    elif [ "${MAIBOT_ENABLE_ADB:-0}" = 1 ] && [ "$(getprop init.svc.adbd)" != running ]; then
        start adbd
    fi
    if ! pidof tailscaled > /dev/null; then
        ts_check_tick=0
        ts_fail_count=0
        "$EDGE_ROOT/tailscale/tailscaled" --tun=userspace-networking \
            --state="$EDGE_ROOT/state/tailscaled.state" \
            --socket="$EDGE_ROOT/run/tailscaled.sock" \
            --socks5-server=127.0.0.1:1055 --outbound-http-proxy-listen=127.0.0.1:1056 \
            >> "$EDGE_ROOT/run/tailscaled.log" 2>&1 &
    else
        ts_check_tick=$((ts_check_tick + 1))
        if [ "$ts_check_tick" -ge 12 ]; then
            ts_check_tick=0
            if "$EDGE_ROOT/tailscale/tailscale" --socket="$EDGE_ROOT/run/tailscaled.sock" status > /dev/null 2>&1 && \
               "$EDGE_ROOT/tailscale/tailscale" --socket="$EDGE_ROOT/run/tailscaled.sock" ping --timeout=5s -c 1 100.87.39.37 > /dev/null 2>&1; then
                ts_fail_count=0
            else
                ts_fail_count=$((ts_fail_count + 1))
                if [ "$ts_fail_count" -ge 3 ]; then
                    killall tailscaled 2>/dev/null || kill -9 $(pidof tailscaled) 2>/dev/null || true
                    ts_fail_count=0
                fi
            fi
        fi
    fi
    sleep 5
done &

PYTHONPATH=/data/adb LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib \
    /data/data/com.termux/files/usr/bin/python3 -m maibot_edge.edge_watchdog \
    >> "$EDGE_ROOT/run/http-watchdog.log" 2>&1 &
