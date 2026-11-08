# Zero-APK notification listener validation

Verified on Xiaomi 10 / Android 13 / MIUI 14 with APatch Root on 2026-11-05.

## Findings

- Plain `getActiveNotifications("com.android.shell")`: UID 2000 rejected with ACCESS_NOTIFICATIONS; UID 0 hit a system NullPointerException. This is not the selected implementation.
- `NotificationTap` uses app_process + DEX and registers an INotificationListener Binder through INotificationManager.registerListener as UID 0. Registration and onListenerConnected succeeded without any APK installation, accessibility grant, notification-access settings mutation, or WeChat process injection.
- Synthetic notification posting and updating produced onNotificationPosted callbacks with notification key, package, postTime, title and text.
- With the phone on its launcher and WeChat in the background, a real user test produced:
  - package: com.tencent.mm
  - title: 做1做0不如做
  - text: [17条]paff: 通知测试123
  - notification key: 0|com.tencent.mm|1511583490|null|10260
- The notification key is a system notification identifier, NOT a verified Chat_User. The 17-message count does not mean the callback contains all 17 messages. Multiple updates and merged summaries require deduplication before creating message records.

## Probe

Sources: NotificationTap.java (live callback), NotificationProbe.java (diagnostic read API).
Existing phone tools: Termux ecj + dx, android.jar. Build with build_probe.sh after pushing NotificationTap.java to /data/local/tmp.

Run a bounded probe:

```sh
CLASSPATH=/data/local/tmp/clip_build/NotificationTap.dex \
  /system/bin/app_process /system/bin NotificationTap
```

Use `timeout 300` for a five-minute validation. The process only prints WeChat and shell test notifications; it never cancels, opens, or replies to notifications. Binder death unregisters the listener when the probe exits.

This is validated capability, not a deployed permanent collector. Next integration needs durable local event IDs, notification update/removal handling, reconciliation after reconnect, group-ID resolution, backpressure, bounded storage, and integration with DMIT. Missing notifications remain invisible to this route. No APK fallback is currently needed.
