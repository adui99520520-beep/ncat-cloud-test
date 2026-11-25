#!/usr/bin/env python3
"""Manual-only login probe. Never logs or persists credentials, UI, or app data."""
import os
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

DEVICE = '127.0.0.1:5555'
PKG = 'com.ncatC200000V261126.T030029'

def adb(*args, capture=False):
    p = subprocess.run(['adb', '-s', DEVICE, *args], stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=45)
    if p.returncode != 0:
        raise RuntimeError('ADB operation failed')
    return p.stdout if capture else b''

def tap(x, y):
    adb('shell', 'input', 'tap', str(x), str(y))

def read_ui():
    adb('shell', 'uiautomator', 'dump', '/sdcard/ui.xml')
    raw = adb('exec-out', 'cat', '/sdcard/ui.xml', capture=True)
    return ET.fromstring(raw)

def node_by_id(root, suffix):
    matches = [n for n in root.iter() if n.get('resource-id', '').endswith(':id/' + suffix)]
    if len(matches) != 1:
        raise RuntimeError('Expected unique app control not found: ' + suffix)
    return matches[0]

def node_tap(node):
    coords = list(map(int, re.findall(r'\d+', node.get('bounds', ''))))
    if len(coords) != 4:
        raise RuntimeError('Invalid control bounds')
    tap((coords[0]+coords[2])//2, (coords[1]+coords[3])//2)

def send_text(value):
    if not value or any(ord(c) < 32 or ord(c) > 126 for c in value) or '%' in value:
        raise RuntimeError('Input cannot be safely typed via the Android input tool')
    # Quote for the Android-side shell; never log input commands or values.
    adb('shell', 'input', 'text', shlex.quote(value.replace(' ', '%s')))

def main():
    username = os.environ.get('NCAT_USERNAME', '')
    password = os.environ.get('NCAT_PASSWORD', '')
    if not username or not password:
        raise RuntimeError('Required repository secrets missing')
    adb('shell', 'am', 'start', '-n', PKG + '/com.salmon.film.main.ui.SplashActivity')
    time.sleep(30)
    tap(648, 1130)
    time.sleep(5)
    ui = read_ui()
    node_tap(node_by_id(ui, 'txt_user'))
    time.sleep(4)
    ui = read_ui()
    node_tap(node_by_id(ui, 'txt_account'))
    send_text(username)
    ui = read_ui()
    if node_by_id(ui, 'txt_account').get('text', '') != username:
        print('LOGIN_STATUS=USERNAME_INPUT_REJECTED_OR_CHANGED')
        sys.exit(1)
    node_tap(node_by_id(ui, 'txt_pwd'))
    send_text(password)
    adb('shell', 'input', 'keyevent', '4')
    ui = read_ui()
    node_tap(node_by_id(ui, 'btn_confirm'))
    time.sleep(12)
    ui = read_ui()
    texts = [n.get('text', '') for n in ui.iter()]
    ids = [n.get('resource-id', '') for n in ui.iter()]
    if any(x.endswith(':id/txt_pwd') for x in ids):
        print('LOGIN_STATUS=NOT_CONFIRMED (login screen remains; do not retry rejected password)')
        for marker in ['验证码', '账号或密码错误', '帐号或密码错误', '账号不存在', '帐号不存在', '密码错误', '登录失败', '请检查网络', '请输入帐号']:
            if any(marker in t for t in texts):
                print('LOGIN_MESSAGE=' + marker)
        sys.exit(1)
    if any('尚未登陆' in t or '尚未登录' in t for t in texts):
        print('LOGIN_STATUS=NOT_CONFIRMED (guest screen)')
        sys.exit(1)
    if any(x.endswith(':id/txt_accelerate') for x in ids) and any(x.endswith(':id/txt_user') for x in ids):
        print('LOGIN_STATUS=APP_ACCOUNT_SCREEN')
        user_label = node_by_id(ui, 'txt_user').get('text', '')
        if not user_label or '尚未登陆' in user_label or '尚未登录' in user_label:
            print('LOGIN_STATUS=ACCOUNT_NOT_AUTHENTICATED')
            sys.exit(1)
        print('CHECKIN_ACTION=CLICK_ONCE')
        node_tap(node_by_id(ui, 'txt_accelerate'))
        time.sleep(8)
        check_ui = read_ui()
        check_texts = [n.get('text', '') for n in check_ui.iter()]
        success_markers = ['签到成功', '今日已签到', '已经签到', '已签到', '签到完成']
        if any(marker in text for marker in success_markers for text in check_texts):
            print('CHECKIN_STATUS=SUCCESS')
            return
        # Toasts are not always present in the accessibility tree; inspect logs in memory only.
        log = adb('logcat', '-d', '-t', '250', capture=True).decode('utf-8', errors='ignore')
        if any(marker in log for marker in success_markers):
            print('CHECKIN_STATUS=SUCCESS')
            return
        if any('尚未登陆' in text or '尚未登录' in text for text in check_texts):
            print('CHECKIN_STATUS=FAILED_AUTH_REQUIRED')
        else:
            print('CHECKIN_STATUS=NOT_CONFIRMED')
        sys.exit(1)
    print('LOGIN_STATUS=NOT_CONFIRMED (unexpected screen)')
    for marker in ['验证码', '登录失败', '登录成功', '安全验证', '验证身份']:
        if any(marker in t for t in texts):
            print('LOGIN_MESSAGE=' + marker)
    sys.exit(1)

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('PROBE_FAILED=' + type(e).__name__)
        if isinstance(e, RuntimeError):
            print('PROBE_REASON=' + str(e))
        sys.exit(1)
