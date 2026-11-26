#!/usr/bin/env python3
"""One controlled check-in using the visible app controls; never logs credentials."""
import importlib.util
import os
import re
import sys
import time

spec = importlib.util.spec_from_file_location('probe', 'scripts/login-probe.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

EXPECTED_USER = 'adui99520520@bot.cue.im'

def texts(root):
    return [v for n in root.iter() for v in (n.get('text', ''), n.get('content-desc', '')) if v]

def success(root):
    ts = texts(root)
    return any('签到成功' in v for v in ts), any('已签到' in v for v in ts)

def main():
    username, password = os.environ.get('NCAT_USERNAME', ''), os.environ.get('NCAT_PASSWORD', '')
    if username != EXPECTED_USER or not password:
        print('RESULT=STOP_ACCOUNT_MISMATCH_OR_MISSING_SECRET')
        return 1
    p.adb('shell', 'am', 'start', '-n', p.PKG + '/com.salmon.film.main.ui.SplashActivity')
    time.sleep(30)
    p.tap(648, 1130)  # Previously verified My tab on the pinned 720x1280 app build.
    time.sleep(5)
    ui = p.read_ui()
    p.node_tap(p.node_by_id(ui, 'txt_user'))
    time.sleep(4)
    ui = p.read_ui()
    p.node_tap(p.node_by_id(ui, 'txt_account'))
    p.send_text(username)
    ui = p.read_ui()
    if p.node_by_id(ui, 'txt_account').get('text', '') != EXPECTED_USER:
        print('RESULT=STOP_USERNAME_INPUT_NOT_CONFIRMED')
        return 1
    p.node_tap(p.node_by_id(ui, 'txt_pwd'))
    p.send_text(password)
    p.adb('shell', 'input', 'keyevent', '4')
    ui = p.read_ui()
    p.node_tap(p.node_by_id(ui, 'btn_confirm'))
    time.sleep(12)
    ui = p.read_ui()
    ids = [n.get('resource-id', '') for n in ui.iter()]
    if any(x.endswith(':id/txt_pwd') for x in ids) or not any(x.endswith(':id/txt_accelerate') for x in ids):
        print('RESULT=STOP_LOGIN_NOT_CONFIRMED')
        return 1
    user = p.node_by_id(ui, 'txt_user').get('text', '')
    if not user or '尚未登陆' in user or '尚未登录' in user:
        print('RESULT=STOP_LOGIN_NOT_CONFIRMED')
        return 1
    good, already = success(ui)
    if already:
        print('RESULT=ALREADY_CHECKED_IN')
        print('ACCOUNT_STATUS=已签到')
        return 0
    button = p.node_by_id(ui, 'txt_accelerate')
    if button.get('text', '') != '今日签到':
        print('RESULT=STOP_INITIAL_STATUS_UNKNOWN')
        return 1
    print('INITIAL_STATUS=今日签到', flush=True)
    p.node_tap(button)  # Exactly one entry tap.
    print('ENTRY_TAPPED_ONCE', flush=True)
    time.sleep(8)  # Wait longer than the approximately 5-second ad countdown.
    candidates = []
    ui = None
    for _ in range(12):
        ui = p.read_ui()
        good, already = success(ui)
        if good or already:
            print('RESULT=SUCCESS_VISIBLE_AFTER_ENTRY')
            print('ACCOUNT_STATUS=' + ('已签到' if already else 'NOT_CONFIRMED'))
            return 0
        candidates = []
        for n in ui.iter():
            if (n.get('text', '').strip() == '点击签到' or n.get('content-desc', '').strip() == '点击签到') and n.get('enabled', 'true') == 'true':
                c = list(map(int, re.findall(r'\d+', n.get('bounds', ''))))
                if len(c) == 4 and c[2] > c[0] and c[3] > c[1] and (c[0]+c[2])//2 >= 360 and (c[1]+c[3])//2 <= 425:
                    candidates.append((tuple(c), n))
        unique = {c: n for c, n in candidates}
        if len(unique) == 1:
            break
        if len(unique) > 1:
            print('RESULT=STOP_AMBIGUOUS_CONFIRM_TARGET')
            return 1
        time.sleep(2)
    if len(unique) != 1:
        print('RESULT=STOP_CONFIRM_TARGET_NOT_FOUND_NO_AD_CLICKED')
        print('ACCOUNT_STATUS=NOT_CONFIRMED')
        return 1
    print('COUNTDOWN_WAITED_CONFIRM_TARGET=EXACT_TOP_RIGHT', flush=True)
    p.node_tap(next(iter(unique.values())))  # Exactly one confirmation tap, never ad content.
    print('CONFIRM_TAPPED_ONCE', flush=True)
    for _ in range(12):
        time.sleep(1)
        ui = p.read_ui()
        good, already = success(ui)
        if good or already:
            print('RESULT=SIGNIN_SUCCESS' if good else 'RESULT=ALREADY_CHECKED_IN')
            print('ACCOUNT_STATUS=' + ('已签到' if already else 'NOT_CONFIRMED'))
            return 0
    print('RESULT=STOP_UNCONFIRMED_AFTER_SINGLE_CONFIRM_TAP')
    print('ACCOUNT_STATUS=NOT_CONFIRMED')
    return 1

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('RESULT=STOP_ERROR_' + type(exc).__name__)
        if isinstance(exc, RuntimeError):
            print('REASON=' + str(exc))
        sys.exit(1)
