#!/bin/bash
set -euo pipefail
curl -fL --retry 2 --max-time 120 -sS -o /tmp/ncat.apk 'https://dl.ncat33.com/target/apk/ncat/202611/26/app/c200000-NcatC200000V261126030029-3.5.1.30501_release.apk'
echo '1de44b3d42f60120d46e621968239c869995c659f2baf6bfb83b3d60539b28a3  /tmp/ncat.apk' | sha256sum -c -
PKG='com.ncatC200000V261126.T030029'
adb install /tmp/ncat.apk
adb logcat -c
adb shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null
sleep 20
if adb shell pidof "$PKG" | grep -q '[0-9]'; then echo 'APP_PROCESS=RUNNING'; else
  echo 'APP_PROCESS=NOT_RUNNING'
  echo 'CRASH_DIAGNOSTICS_BEGIN'
  adb logcat -d -b crash | grep -E 'FATAL EXCEPTION|Process:|java.lang.|UnsatisfiedLinkError|SIG(SEGV|ABRT|ILL)|Caused by:|Fatal signal' | head -25 || true
  echo 'CRASH_DIAGNOSTICS_END'
fi
adb shell uiautomator dump /sdcard/window.xml >/dev/null 2>&1 || true
adb shell cat /sdcard/window.xml > /tmp/ui.xml 2>/dev/null || true
for word in 今日签到 签到 我的 登录; do
  if grep -q "$word" /tmp/ui.xml; then echo "UI_PRESENT=$word"; else echo "UI_ABSENT=$word"; fi
done
