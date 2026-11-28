#!/usr/bin/env python3
"""Read-only account-state probe; never submits credentials or check-in controls."""
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

DEVICE = '127.0.0.1:5555'
PKG = 'com.ncatC200000V261126.T030029'
GUEST_MARKERS = ('尚未登陆', '尚未登录', '请登录', '请登陆', '点击登录', '点击登陆', '登录/注册', '登陆/注册')
SUCCESS_MARKERS = ('签到成功', '今日已签到', '已经签到', '已签到', '签到完成')


def adb(*args, capture=False, timeout=45):
    p = subprocess.run(
        ['adb', '-s', DEVICE, *args],
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    if p.returncode != 0:
        raise RuntimeError('ADB operation failed')
    return p.stdout if capture else b''


def tap(x, y):
    adb('shell', 'input', 'tap', str(x), str(y))


def send_text(value):
    if not value or any(ord(c) < 32 or ord(c) > 126 for c in value) or '%' in value:
        raise RuntimeError('Input cannot be safely typed via the Android input tool')
    # Quote for the Android-side shell; never log input commands or values.
    adb('shell', 'input', 'text', shlex.quote(value.replace(' ', '%s')))


def read_ui(timeout=45):
    adb('shell', 'uiautomator', 'dump', '/sdcard/ui.xml', timeout=timeout)
    raw = adb('exec-out', 'cat', '/sdcard/ui.xml', capture=True, timeout=timeout)
    return ET.fromstring(raw)


def nodes_by_id(root, suffix):
    return [n for n in root.iter() if n.get('resource-id', '').endswith(':id/' + suffix)]


def node_by_id(root, suffix):
    matches = nodes_by_id(root, suffix)
    if len(matches) != 1:
        raise RuntimeError('Expected unique app control not found: ' + suffix)
    return matches[0]


def text_values(root):
    return [value.strip() for node in root.iter() for value in (node.get('text', ''), node.get('content-desc', '')) if value.strip()]


def exact_text_nodes(root, value):
    return [n for n in root.iter() if n.get('text', '').strip() == value or n.get('content-desc', '').strip() == value]


def node_tap(node):
    if node.get('visible-to-user') == 'false' or node.get('enabled') == 'false':
        raise RuntimeError('Navigation control is not available')
    coords = list(map(int, re.findall(r'\d+', node.get('bounds', ''))))
    if len(coords) != 4 or coords[2] <= coords[0] or coords[3] <= coords[1]:
        raise RuntimeError('Invalid navigation control bounds')
    # This function is used only for the uniquely labelled "我的" navigation tab.
    adb('shell', 'input', 'tap', str((coords[0] + coords[2]) // 2), str((coords[1] + coords[3]) // 2))


def account_state(root):
    """Classify only unambiguous, read-only UI evidence; never infer or submit a login."""
    account_fields = nodes_by_id(root, 'txt_account')
    password_fields = nodes_by_id(root, 'txt_pwd')
    if account_fields or password_fields:
        if len(account_fields) == 1 and len(password_fields) == 1:
            return 'LOGIN_FORM'
        return 'AMBIGUOUS_LOGIN_FORM'

    user_nodes = nodes_by_id(root, 'txt_user')
    checkin_nodes = nodes_by_id(root, 'txt_accelerate')
    if len(user_nodes) == 1 and len(checkin_nodes) == 1:
        label = user_nodes[0].get('text', '').strip() or user_nodes[0].get('content-desc', '').strip()
        if not label:
            return 'UNKNOWN'
        if any(marker in label for marker in GUEST_MARKERS):
            return 'LOGGED_OUT'
        return 'AUTHENTICATED'

    if any(marker in value for marker in GUEST_MARKERS for value in text_values(root)):
        return 'LOGGED_OUT'
    return 'UNKNOWN'


def account_ui_after_safe_navigation(ui):
    state = account_state(ui)
    if state != 'UNKNOWN':
        return ui, state

    # Navigate only through one exact, visible "我的" label; never use a blind coordinate.
    my_tabs = exact_text_nodes(ui, '我的')
    if len(my_tabs) != 1:
        return ui, 'UNKNOWN'
    node_tap(my_tabs[0])
    time.sleep(2)
    ui = read_ui()
    return ui, account_state(ui)


def main():
    adb('shell', 'am', 'start', '-n', PKG + '/com.salmon.film.main.ui.SplashActivity')
    time.sleep(30)
    ui = read_ui()
    ui, state = account_ui_after_safe_navigation(ui)

    if state == 'LOGIN_FORM':
        print('LOGIN_STATUS=NOT_AUTHENTICATED_LOGIN_FORM')
        print('SIGNIN_ATTEMPTED=NO')
        return 0
    if state == 'AMBIGUOUS_LOGIN_FORM':
        print('LOGIN_STATUS=AMBIGUOUS_LOGIN_FORM_NO_ACTION')
        return 1
    if state == 'LOGGED_OUT':
        print('LOGIN_STATUS=NOT_AUTHENTICATED_GUEST')
        print('SIGNIN_ATTEMPTED=NO')
        return 0
    if state != 'AUTHENTICATED':
        print('LOGIN_STATUS=UNKNOWN_SCREEN_NO_ACTION')
        return 1

    print('LOGIN_STATUS=APP_ACCOUNT_SCREEN')
    values = text_values(ui)
    button_text = node_by_id(ui, 'txt_accelerate').get('text', '').strip()
    if any(marker in value for marker in SUCCESS_MARKERS for value in values) or any(marker in button_text for marker in SUCCESS_MARKERS):
        print('CHECKIN_STATUS=SUCCESS_ALREADY_RECORDED')
    elif button_text == '今日签到':
        print('CHECKIN_STATUS=STILL_AVAILABLE')
    else:
        print('CHECKIN_STATUS=UNKNOWN_READ_ONLY')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('PROBE_FAILED=' + type(exc).__name__)
        if isinstance(exc, RuntimeError):
            print('PROBE_REASON=' + str(exc))
        sys.exit(1)
