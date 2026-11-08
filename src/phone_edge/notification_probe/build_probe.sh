#!/system/bin/sh
# Run as root on this phone. Uses existing Termux ecj/dx; installs no APK.
set -eu
BUILD=/data/local/tmp/clip_build
TERMUX_UID=$(stat -c %u /data/data/com.termux/files)
cp /data/local/tmp/NotificationTap.java "$BUILD/NotificationTap.java"
chown "$TERMUX_UID:$TERMUX_UID" "$BUILD/NotificationTap.java"
su - "$TERMUX_UID" -c 'export PATH=/data/data/com.termux/files/usr/bin:$PATH; cd /data/local/tmp/clip_build && ecj NotificationTap.java && dx --dex --output=NotificationTap.dex NotificationTap.class'
