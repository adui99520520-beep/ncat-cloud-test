#!/usr/bin/env python3
"""Controlled check-in with minimal, redacted UI evidence. Do not run unless explicitly authorized."""
import importlib.util
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path(os.environ.get('EVIDENCE_DIR', 'checkin-evidence'))
BASE.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location('probe', 'scripts/login-probe.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
EXPECTED_USER = 'adui99520520@bot.cue.im'
USERNAME = ''
PASSWORD = ''
AUTHENTICATED = False


def scrub(s):
    s = str(s or '')
    for secret in (USERNAME, PASSWORD):
        if secret:
            s = s.replace(secret, '[REDACTED]')
    s = re.sub(r'(?i)(password|passwd|pwd|token|secret)(\s*[=:：]\s*)[^\s,;]+', r'\1\2[REDACTED]', s)
    s = re.sub(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[EMAIL_REDACTED]', s)
    return s


def texts(root):
    return [v for n in root.iter() for v in (n.get('text', ''), n.get('content-desc', '')) if v]


def success(root):
    ts = texts(root)
    return any('签到成功' in v for v in ts), any('已签到' in v for v in ts)


def evidence(label, root=None):
    """Best-effort UI dump and screenshot; keep only short, sanitized text."""
    try:
        (BASE / (label + '.png')).write_bytes(p.adb('exec-out', 'screencap', '-p', capture=True))
    except Exception as e:
        print('EVIDENCE_SCREENSHOT_ERROR=' + type(e).__name__, flush=True)
    if root is None:
        try:
            root = p.read_ui()
        except Exception as e:
            print('EVIDENCE_UI_ERROR=' + type(e).__name__, flush=True)
            return
    lines = []
    for n in root.iter():
        text = scrub(n.get('text', '') or n.get('content-desc', '')).strip()
        rid = n.get('resource-id', '').rsplit('/', 1)[-1]
        if text:
            lines.append((rid + ': ' if rid else '') + text[:180])
    content = '\n'.join(lines[:100])[:10000]
    (BASE / (label + '.txt')).write_text(content + ('\n' if content else ''), encoding='utf-8')


def main():
    global USERNAME, PASSWORD, AUTHENTICATED
    USERNAME, PASSWORD = os.environ.get('NCAT_USERNAME', ''), os.environ.get('NCAT_PASSWORD', '')
    if USERNAME != EXPECTED_USER or not PASSWORD:
        print('RESULT=STOP_ACCOUNT_MISMATCH_OR_MISSING_SECRET')
        return 1
    p.adb('shell', 'am', 'start', '-n', p.PKG + '/com.salmon.film.main.ui.SplashActivity')
    time.sleep(30)
    p.tap(648, 1130)
    time.sleep(5)
    ui = p.read_ui()
    p.node_tap(p.node_by_id(ui, 'txt_user'))
    time.sleep(4)
    ui = p.read_ui()
    p.node_tap(p.node_by_id(ui, 'txt_account'))
    p.send_text(USERNAME)
    ui = p.read_ui()
    if p.node_by_id(ui, 'txt_account').get('text', '') != EXPECTED_USER:
        print('RESULT=STOP_USERNAME_INPUT_NOT_CONFIRMED')
        return 1
    p.node_tap(p.node_by_id(ui, 'txt_pwd'))
    p.send_text(PASSWORD)
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
    AUTHENTICATED = True

    # Capture only the authenticated account screen; never preserve the credential-entry screen.
    evidence('01-initial', ui)
    good, already = success(ui)
    if good or already:
        print('INITIAL_STATUS=已签到')
        print('RESULT=ALREADY_CHECKED_IN')
        return 0
    button = p.node_by_id(ui, 'txt_accelerate')
    initial = button.get('text', '').strip()
    if initial != '今日签到':
        print('INITIAL_STATUS=未知')
        print('RESULT=STOP_INITIAL_STATUS_UNKNOWN_NO_CLICK')
        return 1
    print('INITIAL_STATUS=今日签到', flush=True)

    # One entry tap only. Ads are observed/waited out; no ad content is ever tapped.
    p.node_tap(button)
    print('ENTRY_TAPPED_ONCE', flush=True)
    entry_time = time.monotonic()
    time.sleep(0.5)
    ui = p.read_ui()
    evidence('02-after-entry', ui)
    # Wait out the known ad countdown without touching ad content.
    time.sleep(max(0, 8 - (time.monotonic() - entry_time)))
    ui = p.read_ui()
    unique = {}
    for _ in range(30):
        unique = {}
        good, already = success(ui)
        if good or already:
            evidence('03-final', ui)
            print('RESULT=' + ('SIGNIN_SUCCESS' if good else 'ALREADY_CHECKED_IN'))
            return 0
        # If a visible countdown remains, keep waiting and never tap.
        if any(re.search(r'(?:\d+\s*(?:秒|s)(?:后|\s|$)|倒计时\s*\d+)', v, re.I) for v in texts(ui)):
            time.sleep(1)
            ui = p.read_ui()
            continue
        # Accept only one explicit, enabled “点击签到” control in the expected upper-right area.
        for n in ui.iter():
            if (n.get('text', '').strip() == '点击签到' or n.get('content-desc', '').strip() == '点击签到') and n.get('enabled', 'true') == 'true':
                c = list(map(int, re.findall(r'\d+', n.get('bounds', ''))))
                if len(c) == 4 and c[2] > c[0] and c[3] > c[1] and (c[0]+c[2])//2 >= 360 and (c[1]+c[3])//2 <= 425:
                    unique[tuple(c)] = n
        if len(unique) > 1:
            evidence('03-final', ui)
            print('RESULT=STOP_AMBIGUOUS_CONFIRM_TARGET_NO_CONFIRM_TAP')
            return 1
        if len(unique) == 1:
            break
        time.sleep(1)
        ui = p.read_ui()
    if len(unique) != 1:
        evidence('03-final', ui)
        print('RESULT=STOP_CONFIRM_TARGET_NOT_FOUND_NO_AD_CLICK')
        return 1

    # Save the pre-confirm screen, make exactly one tap, then capture immediately.
    evidence('02b-before-confirm', ui)
    expected_bounds = next(iter(unique.keys()))
    ui = p.read_ui()
    if any(re.search(r'(?:\d+\s*(?:秒|s)(?:后|\s|$)|倒计时\s*\d+)', v, re.I) for v in texts(ui)):
        print('RESULT=STOP_COUNTDOWN_REAPPEARED_NO_CONFIRM_TAP')
        return 1
    candidates = []
    for n in ui.iter():
        if (n.get('text', '').strip() == '点击签到' or n.get('content-desc', '').strip() == '点击签到') and n.get('enabled', 'true') == 'true':
            c = tuple(map(int, re.findall(r'\d+', n.get('bounds', ''))))
            if len(c) == 4 and c == expected_bounds:
                candidates.append(n)
    if len(candidates) != 1:
        print('RESULT=STOP_CONFIRM_TARGET_CHANGED_NO_CONFIRM_TAP')
        return 1
    print('COUNTDOWN_WAITED_CONFIRM_TARGET=EXACT_TOP_RIGHT', flush=True)
    p.node_tap(candidates[0])
    print('CONFIRM_TAPPED_ONCE', flush=True)
    time.sleep(0.25)
    try:
        evidence('03-immediate-after-confirm')
        ui = p.read_ui()
    except Exception:
        ui = None
    for _ in range(12):
        time.sleep(1)
        try:
            ui = p.read_ui()
            good, already = success(ui)
            if good or already:
                evidence('04-final', ui)
                print('RESULT=' + ('SIGNIN_SUCCESS' if good else 'ALREADY_CHECKED_IN'))
                return 0
        except Exception:
            continue
    evidence('04-final', ui)
    print('RESULT=STOP_UNCONFIRMED_AFTER_SINGLE_CONFIRM_TAP')
    return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('RESULT=STOP_ERROR_' + type(exc).__name__)
        if isinstance(exc, RuntimeError):
            print('REASON=' + scrub(str(exc)))
        # Never capture the credential-entry screen on an early failure.
        if AUTHENTICATED:
            evidence('99-error')
        sys.exit(1)
