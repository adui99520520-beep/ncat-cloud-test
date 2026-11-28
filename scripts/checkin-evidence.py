#!/usr/bin/env python3
"""Controlled check-in with private, encrypted evidence. Never logs credentials."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path(os.environ.get('EVIDENCE_DIR', 'checkin-evidence'))
BASE.mkdir(parents=True, exist_ok=True)
COUNTDOWN_LOG = BASE / 'countdown-observations.txt'
COUNTDOWN_LOG.write_text('Observed countdown values after entry tap:\n', encoding='utf-8')
spec = importlib.util.spec_from_file_location('probe', 'scripts/login-probe.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

EXPECTED_USER = 'adui99520520@bot.cue.im'
ALLOWED_SERVERS = ('精品1', '精品2', '精品3', '优化1', '精品5')
APP_UI_READY_TIMEOUT = 90
APP_UI_POLL_INTERVAL = 2
APP_UI_STARTUP_DELAY = 30
APP_UI_COMMAND_TIMEOUT = 45
# Prefer the last known working line, but start with the app's current line and
# visit every other supported line at most once.
PREFERRED_SERVERS = ('精品3', '精品1', '精品2', '优化1', '精品5')
USERNAME = ''
PASSWORD = ''
AUTHENTICATED = False
EXPECTED_ACCOUNT_VERIFIED_THIS_RUN = False
LOGIN_USERNAME_VERIFIED_THIS_RUN = False
INITIAL_STATUS_AVAILABLE = False
ATTEMPTED_SERVERS = set()
IDENTITY_BLOCKED = False
CHECKIN_MODE = 'checkin'
SUMMARY_MODE = 'checkin'
SUMMARY_IDENTITY_STATUS = 'UNKNOWN'
SUMMARY_TODAY_STATUS = 'NOT_READ'
SUMMARY_ATTEMPTED_ROUTES = []
ENTRY_CLICK_ATTEMPTED = False
CONFIRM_CLICK_ATTEMPTED = False
SUMMARY_RESULT_CODE = 'NOT_STARTED'
SUMMARY_RESULT_CODES = frozenset((
    'NOT_STARTED', 'STOP_INVALID_MODE', 'STOP_ACCOUNT_CONFIG',
    'STOP_IDENTITY_UNVERIFIED', 'STOP_APP_UI_UNKNOWN', 'STOP_STATUS_UNKNOWN',
    'ALREADY_CHECKED_IN', 'VERIFY_STATUS_AVAILABLE', 'CHECKIN_IN_PROGRESS',
    'CHECKIN_COMPLETE', 'CHECKIN_STOP', 'STOP_ERROR', 'STOP_NETWORK_IO_EXHAUSTED',
))


def identity_summary_code(state):
    if state == 'AUTHENTICATED_EXPECTED':
        return 'VERIFIED_EXPECTED'
    if state in ('AUTHENTICATED_OTHER_ACCOUNT', 'OTHER_EMAIL_VISIBLE'):
        return 'OTHER_ACCOUNT'
    if state == 'AUTHENTICATED_IDENTITY_UNVERIFIED':
        return 'UNVERIFIED'
    return 'UNKNOWN'


def write_step_summary():
    """Append only allowlisted status codes, supported routes, and click booleans."""
    summary_path = os.environ.get('GITHUB_STEP_SUMMARY', '')
    if not summary_path:
        return
    mode = SUMMARY_MODE if SUMMARY_MODE in ('checkin', 'verify', 'invalid') else 'invalid'
    identity = SUMMARY_IDENTITY_STATUS if SUMMARY_IDENTITY_STATUS in (
        'UNKNOWN', 'VERIFIED_EXPECTED', 'UNVERIFIED', 'OTHER_ACCOUNT'
    ) else 'UNKNOWN'
    status = SUMMARY_TODAY_STATUS if SUMMARY_TODAY_STATUS in (
        'NOT_READ', 'AVAILABLE', 'SIGNED_IN', 'UNKNOWN'
    ) else 'UNKNOWN'
    routes = []
    for server in SUMMARY_ATTEMPTED_ROUTES:
        if server in ALLOWED_SERVERS and server not in routes:
            routes.append(server)
            if len(routes) == 5:
                break
    result = SUMMARY_RESULT_CODE if SUMMARY_RESULT_CODE in SUMMARY_RESULT_CODES else 'UNKNOWN'
    lines = (
        'mode=' + mode,
        'identity_status=' + identity,
        'today_status=' + status,
        'attempted_routes=' + (','.join(routes) if routes else 'none'),
        'entry_click_attempted=' + ('yes' if ENTRY_CLICK_ATTEMPTED else 'no'),
        'confirmation_click_attempted=' + ('yes' if CONFIRM_CLICK_ATTEMPTED else 'no'),
        'result_code=' + result,
    )
    try:
        with open(summary_path, 'a', encoding='utf-8') as output:
            output.write('\n'.join(lines) + '\n')
    except Exception:
        print('STEP_SUMMARY_WRITE=FAILED', flush=True)


def record_route_attempt(server):
    """Record a route only when its check-in entry tap is about to be attempted."""
    if server not in ALLOWED_SERVERS or server in SUMMARY_ATTEMPTED_ROUTES:
        return False
    if len(SUMMARY_ATTEMPTED_ROUTES) >= 5:
        return False
    SUMMARY_ATTEMPTED_ROUTES.append(server)
    return True


def scrub(value):
    value = str(value or '')
    for secret in (USERNAME, PASSWORD):
        if secret:
            value = value.replace(secret, '[REDACTED]')
    value = re.sub(r'(?i)(password|passwd|pwd|token|secret)(\s*[=:：]\s*)[^\s,;]+', r'\1\2[REDACTED]', value)
    value = re.sub(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[EMAIL_REDACTED]', value)
    return value


def texts(root):
    if root is None:
        return []
    return [value for node in root.iter() for value in (node.get('text', ''), node.get('content-desc', '')) if value]


def success(root):
    values = texts(root)
    return any(any(marker in value for marker in ('签到成功', '今日已签到', '已经签到', '已签到', '签到完成')) for value in values)


def compact_text(value):
    return re.sub(r'\s+', '', str(value or '')).lower()


def has_network_io_error(value):
    value = compact_text(value)
    return bool(re.search(r'网络[i1l]/?[o0]异常|network[i1l]/?[o0]exception', value, re.I)) or '网络io异常' in value


def image_has_network_io_error(image_path):
    """OCR the disposable screenshot in memory; never emit OCR text to Actions logs."""
    if not image_path or not Path(image_path).is_file():
        return False
    try:
        result = subprocess.run(
            ['tesseract', str(image_path), 'stdout', '-l', 'chi_sim+eng', '--psm', '11'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return has_network_io_error(result.stdout.decode('utf-8', errors='ignore'))
    except (OSError, subprocess.SubprocessError):
        return False


def network_io_error(root=None, image_path=None, inspect_logcat=True):
    if any(has_network_io_error(value) for value in texts(root)):
        return True
    if image_has_network_io_error(image_path):
        return True
    if inspect_logcat:
        try:
            logcat = p.adb('logcat', '-d', '-t', '350', capture=True).decode('utf-8', errors='ignore')
            if has_network_io_error(logcat):
                return True
        except Exception:
            pass
    return False


def clear_logcat():
    try:
        p.adb('logcat', '-c')
    except Exception:
        pass


def countdown_values(root):
    values = []
    pattern = re.compile(r'(?:倒计时\s*[:：]?\s*)?(\d+)\s*(秒|s)(?:后)?|倒计时\s*[:：]?\s*(\d+)', re.I)
    for value in texts(root):
        for match in pattern.finditer(value):
            number = match.group(1) or match.group(3)
            unit = match.group(2) or ''
            label = number + unit
            if label not in values:
                values.append(label)
    return values


def record_countdown(root, entry_time, seen):
    for value in countdown_values(root):
        if value in seen:
            continue
        seen.add(value)
        line = f't+{time.monotonic() - entry_time:.2f}s {value}'
        with COUNTDOWN_LOG.open('a', encoding='utf-8') as output:
            output.write(line + '\n')
        print('COUNTDOWN_OBSERVED=' + value, flush=True)


def account_screen(root):
    ids = [node.get('resource-id', '') for node in root.iter()]
    return any(value.endswith(':id/txt_accelerate') for value in ids) and any(value.endswith(':id/txt_user') for value in ids)


def account_identity_status(root):
    if not account_screen(root):
        return 'NOT_ACCOUNT_SCREEN'
    expected = EXPECTED_USER.casefold()
    values = [
        value.strip()
        for node in root.iter()
        for value in (node.get('text', ''), node.get('content-desc', ''))
        if value.strip()
    ]
    emails = [email.casefold() for value in values for email in re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', value)]
    if any(email != expected for email in emails):
        return 'OTHER_ACCOUNT'
    if expected in emails:
        return 'EXPECTED_ACCOUNT'
    return 'UNVERIFIED'


def visible_other_email(root):
    expected = EXPECTED_USER.casefold()
    return any(
        email.casefold() != expected
        for value in texts(root)
        for email in re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', value)
    )


def block_on_other_email(root):
    global IDENTITY_BLOCKED, SUMMARY_IDENTITY_STATUS
    if visible_other_email(root):
        IDENTITY_BLOCKED = True
        SUMMARY_IDENTITY_STATUS = 'OTHER_ACCOUNT'
        return True
    return False


def authenticated_account_screen(root):
    if not account_screen(root):
        return False
    identity = account_identity_status(root)
    if identity == 'OTHER_ACCOUNT':
        return False
    if identity == 'EXPECTED_ACCOUNT':
        return True
    return EXPECTED_ACCOUNT_VERIFIED_THIS_RUN


def account_status(root):
    if not account_screen(root):
        return 'NOT_ACCOUNT_SCREEN'
    if success(root):
        return 'SIGNED_IN'
    try:
        button = p.node_by_id(root, 'txt_accelerate').get('text', '').strip()
    except Exception:
        return 'UNKNOWN'
    if button == '今日签到':
        return 'AVAILABLE'
    if '已签到' in button:
        return 'SIGNED_IN'
    return 'UNKNOWN'


def classify_account_ui(root):
    if root is None:
        return 'UNKNOWN'
    state = p.account_state(root)
    if state == 'AUTHENTICATED':
        identity = account_identity_status(root)
        if identity == 'EXPECTED_ACCOUNT':
            return 'AUTHENTICATED_EXPECTED'
        if identity == 'OTHER_ACCOUNT':
            return 'AUTHENTICATED_OTHER_ACCOUNT'
        if EXPECTED_ACCOUNT_VERIFIED_THIS_RUN and account_screen(root):
            return 'AUTHENTICATED_EXPECTED'
        return 'AUTHENTICATED_IDENTITY_UNVERIFIED'
    if visible_other_email(root):
        return 'OTHER_EMAIL_VISIBLE'
    return state


def wait_for_account_ui(timeout=APP_UI_READY_TIMEOUT):
    """Wait only a bounded time for an unambiguous account or login UI."""
    deadline = time.monotonic() + max(0.0, timeout)
    last_error_class = ''
    attempted_my_navigation = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        call_timeout = max(0.1, min(APP_UI_COMMAND_TIMEOUT, remaining / 2))
        try:
            ui = p.read_ui(timeout=call_timeout)
            state = classify_account_ui(ui)
            if state != 'UNKNOWN':
                return ui, state
            if not attempted_my_navigation:
                my_tabs = p.exact_text_nodes(ui, '我的')
                if len(my_tabs) == 1:
                    attempted_my_navigation = True
                    p.node_tap(my_tabs[0])
        except Exception as exc:
            last_error_class = type(exc).__name__
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(APP_UI_POLL_INTERVAL, remaining))
    print('APP_UI_WAIT=TIMEOUT', flush=True)
    if last_error_class:
        print('APP_UI_LAST_ERROR_CLASS=' + last_error_class, flush=True)
    return None, 'UNKNOWN'


def start_app_and_wait_for_account_ui():
    p.adb('shell', 'am', 'start', '-n', p.PKG + '/com.salmon.film.main.ui.SplashActivity')
    time.sleep(APP_UI_STARTUP_DELAY)
    return wait_for_account_ui()


def exact_text_nodes(root, value):
    return [node for node in root.iter() if node.get('text', '').strip() == value or node.get('content-desc', '').strip() == value]


def node_bounds(node):
    values = list(map(int, re.findall(r'\d+', node.get('bounds', ''))))
    return tuple(values) if len(values) == 4 else None


def tap_unique_text(root, value):
    matches = exact_text_nodes(root, value)
    if len(matches) != 1:
        return False
    bounds = node_bounds(matches[0])
    if not bounds or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        return False
    p.node_tap(matches[0])
    return True


def capture_private_screenshot(label):
    """Save only a screenshot to the private run directory; the workflow encrypts it."""
    image_path = BASE / (label + '.png')
    try:
        image_path.write_bytes(p.adb('exec-out', 'screencap', '-p', capture=True))
    except Exception as exc:
        print('EVIDENCE_SCREENSHOT_ERROR=' + type(exc).__name__, flush=True)
        image_path = None
    return image_path


def evidence(label, root=None):
    """Save UI evidence only to the private run directory; encrypt it before upload."""
    image_path = capture_private_screenshot(label)
    if root is None:
        try:
            root = p.read_ui()
        except Exception as exc:
            print('EVIDENCE_UI_ERROR=' + type(exc).__name__, flush=True)
            return image_path
    lines = []
    for node in root.iter():
        value = scrub(node.get('text', '') or node.get('content-desc', '')).strip()
        resource_id = node.get('resource-id', '').rsplit('/', 1)[-1]
        if value:
            lines.append((resource_id + ': ' if resource_id else '') + value[:180])
    content = '\n'.join(lines[:100])[:10000]
    (BASE / (label + '.txt')).write_text(content + ('\n' if content else ''), encoding='utf-8')
    return image_path


def read_authenticated_account_screen():
    """Return to the user's account tab without touching any ad or check-in control."""
    global IDENTITY_BLOCKED, SUMMARY_IDENTITY_STATUS
    for _ in range(3):
        ui, state = wait_for_account_ui(timeout=30)
        if state == 'AUTHENTICATED_EXPECTED' and authenticated_account_screen(ui):
            SUMMARY_IDENTITY_STATUS = 'VERIFIED_EXPECTED'
            return ui
        if state in ('AUTHENTICATED_OTHER_ACCOUNT', 'AUTHENTICATED_IDENTITY_UNVERIFIED', 'OTHER_EMAIL_VISIBLE', 'LOGIN_FORM', 'LOGGED_OUT', 'AMBIGUOUS_LOGIN_FORM'):
            IDENTITY_BLOCKED = True
            SUMMARY_IDENTITY_STATUS = identity_summary_code(state)
            return None
        try:
            p.adb('shell', 'input', 'keyevent', '4')
            time.sleep(1.5)
        except Exception:
            break
    return None


def open_settings_from_account():
    ui = read_authenticated_account_screen()
    if ui is None:
        return None
    if not tap_unique_text(ui, '应用设置'):
        return None
    time.sleep(1.5)
    try:
        settings = p.read_ui()
        if block_on_other_email(settings):
            print('ACCOUNT_IDENTITY=STOP_OTHER_EMAIL_VISIBLE', flush=True)
            return None
        line = p.node_by_id(settings, 'txt_line')
        if line.get('text', '').strip() not in ALLOWED_SERVERS:
            return None
        return settings
    except Exception:
        return None


def go_back_to_account():
    try:
        p.adb('shell', 'input', 'keyevent', '4')
        time.sleep(1.5)
    except Exception:
        return None
    return read_authenticated_account_screen()


def server_order(current):
    if current not in ALLOWED_SERVERS:
        return []
    return [current] + [server for server in PREFERRED_SERVERS if server != current]


def switch_server(target):
    """Select one exact supported server and verify the app reflects that choice."""
    if CHECKIN_MODE != 'checkin':
        print('LINE_SWITCH=STOP_MODE_NOT_CHECKIN', flush=True)
        return 'STOP', None
    settings = open_settings_from_account()
    if settings is None:
        print('LINE_SWITCH=STOP_SETTINGS_NOT_VERIFIED', flush=True)
        return 'STOP', None
    try:
        current = p.node_by_id(settings, 'txt_line').get('text', '').strip()
    except Exception:
        print('LINE_SWITCH=STOP_CURRENT_LINE_UNKNOWN', flush=True)
        return 'STOP', None
    if current not in ALLOWED_SERVERS or target not in ALLOWED_SERVERS:
        print('LINE_SWITCH=STOP_UNSUPPORTED_LINE', flush=True)
        return 'STOP', None
    if current != target:
        if not tap_unique_text(settings, current):
            print('LINE_SWITCH=STOP_PICKER_ENTRY_NOT_UNIQUE', flush=True)
            return 'STOP', None
        time.sleep(1)
        try:
            picker = p.read_ui()
        except Exception:
            print('LINE_SWITCH=STOP_PICKER_NOT_READABLE', flush=True)
            return 'STOP', None
        if block_on_other_email(picker):
            print('ACCOUNT_IDENTITY=STOP_OTHER_EMAIL_VISIBLE', flush=True)
            return 'STOP', None
        if '选择应用服务器' not in texts(picker):
            error = network_io_error(picker)
            print('LINE_SWITCH=' + ('NETWORK_IO' if error else 'STOP_PICKER_NOT_VERIFIED'), flush=True)
            try:
                p.adb('shell', 'input', 'keyevent', '4')
                time.sleep(1)
            except Exception:
                pass
            return ('NETWORK_IO' if error else 'STOP'), None
        options = exact_text_nodes(picker, target)
        if len(options) != 1:
            # 精品5 is at the bottom edge on the known 720x1280 build. Scroll
            # only inside the server list, then re-read and require one exact row.
            try:
                p.adb('shell', 'input', 'swipe', '360', '1110', '360', '720', '450')
                time.sleep(0.5)
                picker = p.read_ui()
                if block_on_other_email(picker):
                    print('ACCOUNT_IDENTITY=STOP_OTHER_EMAIL_VISIBLE', flush=True)
                    return 'STOP', None
                options = exact_text_nodes(picker, target)
            except Exception:
                options = []
        if len(options) != 1:
            print('LINE_SWITCH=STOP_TARGET_NOT_UNIQUE', flush=True)
            try:
                p.adb('shell', 'input', 'keyevent', '4')
                time.sleep(1)
            except Exception:
                pass
            return 'STOP', None
        bounds = node_bounds(options[0])
        if not bounds or (bounds[0] + bounds[2]) // 2 < 0 or (bounds[0] + bounds[2]) // 2 >= 720 or (bounds[1] + bounds[3]) // 2 >= 1180:
            print('LINE_SWITCH=STOP_TARGET_NOT_VISIBLE', flush=True)
            return 'STOP', None
        p.node_tap(options[0])
        time.sleep(1.5)
        try:
            settings = p.read_ui()
            if block_on_other_email(settings):
                print('ACCOUNT_IDENTITY=STOP_OTHER_EMAIL_VISIBLE', flush=True)
                return 'STOP', None
            chosen = p.node_by_id(settings, 'txt_line').get('text', '').strip()
        except Exception:
            print('LINE_SWITCH=STOP_SELECTED_LINE_UNREADABLE', flush=True)
            return 'STOP', None
        if chosen != target:
            error = network_io_error(settings)
            print('LINE_SWITCH=' + ('NETWORK_IO' if error else 'STOP_SELECTION_NOT_CONFIRMED'), flush=True)
            try:
                p.adb('shell', 'input', 'keyevent', '4')
                time.sleep(1)
            except Exception:
                pass
            return ('NETWORK_IO' if error else 'STOP'), None
    print('LINE_SELECTED=' + target, flush=True)
    ui = go_back_to_account()
    if ui is None:
        print('LINE_SWITCH=STOP_ACCOUNT_SCREEN_NOT_REACHED', flush=True)
        return 'STOP', None
    return 'OK', ui


def persistent_status_after_attempt(server):
    global SUMMARY_TODAY_STATUS
    ui = read_authenticated_account_screen()
    if ui is None:
        SUMMARY_TODAY_STATUS = 'UNKNOWN'
        return 'UNREAD', None
    evidence('04-persistent-account-status-' + server, ui)
    if not authenticated_account_screen(ui):
        return 'UNREAD', ui
    status = account_status(ui)
    SUMMARY_TODAY_STATUS = status if status in ('SIGNED_IN', 'AVAILABLE') else 'UNKNOWN'
    if status == 'SIGNED_IN':
        print('PERSISTENT_STATUS=已签到', flush=True)
    elif status == 'AVAILABLE':
        print('PERSISTENT_STATUS=今日签到', flush=True)
    else:
        print('PERSISTENT_STATUS=UNKNOWN', flush=True)
    return status, ui


def find_confirm_target(root):
    matches = {}
    for node in root.iter():
        label = node.get('text', '').strip() or node.get('content-desc', '').strip()
        if label != '点击签到' or node.get('enabled', 'false') != 'true' or node.get('visible-to-user', 'false') != 'true':
            continue
        bounds = node_bounds(node)
        if not bounds:
            continue
        left, top, right, bottom = bounds
        center_x, center_y = (left + right) // 2, (top + bottom) // 2
        if right > left and bottom > top and center_x >= 360 and center_y <= 425:
            matches[tuple(bounds)] = node
    return matches


def retry_decision_after_network_error(server, ui=None, image_path=None):
    if IDENTITY_BLOCKED:
        print('RESULT=STOP_ACCOUNT_IDENTITY_BLOCKED_NO_ROUTE_RETRY', flush=True)
        return 1
    if not network_io_error(ui, image_path):
        return None
    print('NETWORK_IO_ERROR_DETECTED=YES', flush=True)
    status, _ = persistent_status_after_attempt(server)
    if status == 'SIGNED_IN':
        print('RESULT=SIGNIN_SUCCESS_PERSISTENT', flush=True)
        return 0
    if status == 'AVAILABLE' or status == 'UNREAD':
        # The next server gets a fresh read-only status check before any tap.
        print('NETWORK_IO_RETRY_NEXT_DISTINCT_LINE=' + server, flush=True)
        return 'RETRY'
    print('RESULT=STOP_PERSISTENT_STATUS_UNKNOWN_AFTER_NETWORK_IO', flush=True)
    return 1


def attempt_on_server(server, initial_ui):
    """Try one line once; return RETRY only for a detected network-I/O exception."""
    global ENTRY_CLICK_ATTEMPTED, CONFIRM_CLICK_ATTEMPTED
    if CHECKIN_MODE != 'checkin':
        print('RESULT=STOP_MODE_NOT_CHECKIN_NO_CLICK', flush=True)
        return 1
    if not AUTHENTICATED or not authenticated_account_screen(initial_ui):
        print('RESULT=STOP_EXPECTED_ACCOUNT_NOT_VERIFIED_NO_CLICK', flush=True)
        return 1
    if not INITIAL_STATUS_AVAILABLE:
        print('RESULT=STOP_INITIAL_TODAY_STATUS_NOT_VERIFIED_NO_CLICK', flush=True)
        return 1
    status = account_status(initial_ui)
    if status == 'SIGNED_IN':
        print('RESULT=ALREADY_CHECKED_IN', flush=True)
        print('PERSISTENT_STATUS=已签到', flush=True)
        return 0
    if status != 'AVAILABLE':
        print('INITIAL_STATUS=未知', flush=True)
        print('RESULT=STOP_INITIAL_STATUS_UNKNOWN_NO_CLICK', flush=True)
        return 1
    if server not in ALLOWED_SERVERS:
        print('RESULT=STOP_UNSUPPORTED_LINE_NO_CLICK', flush=True)
        return 1
    if server in ATTEMPTED_SERVERS:
        print('RESULT=STOP_DUPLICATE_LINE_BLOCKED', flush=True)
        return 1

    # Mark the line before the first check-in entry tap. No code path can tap it again.
    ATTEMPTED_SERVERS.add(server)
    print('LINE_ATTEMPT_STARTED=' + server, flush=True)
    print('INITIAL_STATUS=今日签到', flush=True)
    clear_logcat()
    button = p.node_by_id(initial_ui, 'txt_accelerate')
    record_route_attempt(server)
    ENTRY_CLICK_ATTEMPTED = True
    p.node_tap(button)
    print('ENTRY_TAPPED_ONCE=' + server, flush=True)
    entry_time = time.monotonic()
    seen_countdowns = set()
    time.sleep(0.5)
    ui = p.read_ui()
    if block_on_other_email(ui):
        print('RESULT=STOP_OTHER_EMAIL_VISIBLE_NO_FURTHER_TAPS', flush=True)
        return 1
    image_path = evidence('02-after-entry-' + server, ui)
    record_countdown(ui, entry_time, seen_countdowns)
    if success(ui):
        final = persistent_status_after_attempt(server)[0]
        if final == 'SIGNED_IN':
            print('RESULT=SIGNIN_SUCCESS_PERSISTENT', flush=True)
            return 0
        print('RESULT=STOP_SUCCESS_TOAST_NOT_PERSISTENT', flush=True)
        return 1
    decision = retry_decision_after_network_error(server, ui, image_path)
    if decision is not None:
        return decision

    # Wait at least the full known ad countdown window; never interact with ad content.
    countdown_deadline = entry_time + 8
    while time.monotonic() < countdown_deadline:
        time.sleep(min(1.0, max(0.05, countdown_deadline - time.monotonic())))
        ui = p.read_ui()
        if block_on_other_email(ui):
            print('RESULT=STOP_OTHER_EMAIL_VISIBLE_NO_FURTHER_TAPS', flush=True)
            return 1
        record_countdown(ui, entry_time, seen_countdowns)
        if success(ui):
            status = persistent_status_after_attempt(server)[0]
            if status == 'SIGNED_IN':
                print('RESULT=SIGNIN_SUCCESS_PERSISTENT', flush=True)
                return 0
            print('RESULT=STOP_SUCCESS_TOAST_NOT_PERSISTENT', flush=True)
            return 1
        decision = retry_decision_after_network_error(server, ui)
        if decision is not None:
            return decision

    unique = {}
    for _ in range(45):
        if block_on_other_email(ui):
            print('RESULT=STOP_OTHER_EMAIL_VISIBLE_NO_FURTHER_TAPS', flush=True)
            return 1
        record_countdown(ui, entry_time, seen_countdowns)
        if success(ui):
            status = persistent_status_after_attempt(server)[0]
            if status == 'SIGNED_IN':
                print('RESULT=SIGNIN_SUCCESS_PERSISTENT', flush=True)
                return 0
            print('RESULT=STOP_SUCCESS_TOAST_NOT_PERSISTENT', flush=True)
            return 1
        decision = retry_decision_after_network_error(server, ui)
        if decision is not None:
            return decision
        if countdown_values(ui):
            time.sleep(1)
            ui = p.read_ui()
            if block_on_other_email(ui):
                print('RESULT=STOP_OTHER_EMAIL_VISIBLE_NO_FURTHER_TAPS', flush=True)
                return 1
            record_countdown(ui, entry_time, seen_countdowns)
            continue
        unique = find_confirm_target(ui)
        if len(unique) > 1:
            evidence('03-final-' + server, ui)
            print('RESULT=STOP_AMBIGUOUS_CONFIRM_TARGET_NO_CONFIRM_TAP', flush=True)
            return 1
        if len(unique) == 1:
            break
        time.sleep(1)
        ui = p.read_ui()
        if block_on_other_email(ui):
            print('RESULT=STOP_OTHER_EMAIL_VISIBLE_NO_FURTHER_TAPS', flush=True)
            return 1

    if len(unique) != 1:
        image_path = evidence('03-final-' + server, ui)
        decision = retry_decision_after_network_error(server, ui, image_path)
        if decision is not None:
            return decision
        print('RESULT=STOP_CONFIRM_TARGET_NOT_FOUND_NO_AD_CLICK', flush=True)
        return 1

    # Re-read before tapping and require the exact same unique control and no countdown.
    evidence('02b-before-confirm-' + server, ui)
    expected_bounds = next(iter(unique.keys()))
    ui = p.read_ui()
    if countdown_values(ui):
        record_countdown(ui, entry_time, seen_countdowns)
        print('RESULT=STOP_COUNTDOWN_REAPPEARED_NO_CONFIRM_TAP', flush=True)
        return 1
    if block_on_other_email(ui):
        print('RESULT=STOP_OTHER_EMAIL_VISIBLE_NO_CONFIRM_TAP', flush=True)
        return 1
    candidates = find_confirm_target(ui)
    if len(candidates) != 1 or next(iter(candidates.keys())) != expected_bounds:
        print('RESULT=STOP_CONFIRM_TARGET_CHANGED_NO_CONFIRM_TAP', flush=True)
        return 1
    if not AUTHENTICATED or not INITIAL_STATUS_AVAILABLE:
        print('RESULT=STOP_ACCOUNT_OR_INITIAL_STATUS_NOT_VERIFIED_NO_CONFIRM_TAP', flush=True)
        return 1
    print('COUNTDOWN_WAITED_CONFIRM_TARGET=EXACT_TOP_RIGHT', flush=True)
    CONFIRM_CLICK_ATTEMPTED = True
    p.node_tap(next(iter(candidates.values())))
    confirm_time = time.monotonic()
    print('CONFIRM_TAPPED_ONCE=' + server, flush=True)

    time.sleep(max(0.0, 0.4 - (time.monotonic() - confirm_time)))
    ui = p.read_ui()
    image_path = evidence('03-immediate-after-confirm-' + server, ui)
    error_seen = network_io_error(ui, image_path)
    if error_seen:
        print('NETWORK_IO_ERROR_DETECTED=YES', flush=True)
    if success(ui):
        print('IMMEDIATE_SUCCESS_TEXT=YES', flush=True)
    else:
        print('IMMEDIATE_SUCCESS_TEXT=NO', flush=True)

    # Observe the toast window briefly, checking UI, logcat, and private screenshot OCR.
    for index in range(4):
        if time.monotonic() - confirm_time >= 2.8:
            break
        time.sleep(0.45)
        ui = p.read_ui()
        screenshot_path = None
        if index in (1, 3):
            screenshot_path = evidence('03b-toast-window-' + server, ui)
        if network_io_error(ui, screenshot_path):
            error_seen = True
            print('NETWORK_IO_ERROR_DETECTED=YES', flush=True)
        if success(ui):
            print('TOAST_WINDOW_SUCCESS_TEXT=YES', flush=True)

    # Always verify the persistent personal/account status before deciding whether to rotate.
    status, _ = persistent_status_after_attempt(server)
    if status == 'SIGNED_IN':
        print('RESULT=SIGNIN_SUCCESS_PERSISTENT', flush=True)
        return 0
    if status == 'AVAILABLE' and error_seen:
        print('NETWORK_IO_RETRY_NEXT_DISTINCT_LINE=' + server, flush=True)
        return 'RETRY'
    if status == 'UNREAD':
        print('RESULT=STOP_PERSISTENT_STATUS_UNREAD', flush=True)
        return 1
    if status == 'AVAILABLE':
        print('RESULT=STOP_UNCONFIRMED_AFTER_SINGLE_CONFIRM_TAP', flush=True)
        return 1
    print('RESULT=STOP_PERSISTENT_STATUS_UNKNOWN', flush=True)
    return 1


def login_expected_account(ui, state):
    """Sign in only from a uniquely recognized login form using the fixed bot account."""
    global EXPECTED_ACCOUNT_VERIFIED_THIS_RUN, LOGIN_USERNAME_VERIFIED_THIS_RUN
    if state == 'LOGGED_OUT':
        user_nodes = p.nodes_by_id(ui, 'txt_user')
        if len(user_nodes) != 1:
            print('RESULT=STOP_GUEST_LOGIN_ENTRY_NOT_UNIQUE', flush=True)
            return None, 'UNKNOWN'
        p.node_tap(user_nodes[0])
        ui, state = wait_for_account_ui()
    if state != 'LOGIN_FORM':
        print('RESULT=STOP_LOGIN_FORM_NOT_VERIFIED', flush=True)
        return None, state

    account_nodes = p.nodes_by_id(ui, 'txt_account')
    password_nodes = p.nodes_by_id(ui, 'txt_pwd')
    if len(account_nodes) != 1 or len(password_nodes) != 1:
        print('RESULT=STOP_LOGIN_FIELDS_NOT_UNIQUE', flush=True)
        return None, 'UNKNOWN'
    account_field = account_nodes[0]
    current_account = account_field.get('text', '').strip()
    if current_account:
        print('RESULT=STOP_USERNAME_FIELD_NOT_EMPTY_BEFORE_INPUT', flush=True)
        return None, 'UNKNOWN'
    p.node_tap(account_field)
    p.send_text(USERNAME)
    ui = p.read_ui(timeout=APP_UI_COMMAND_TIMEOUT)
    if classify_account_ui(ui) != 'LOGIN_FORM':
        print('RESULT=STOP_LOGIN_FORM_CHANGED_BEFORE_SUBMIT', flush=True)
        return None, 'UNKNOWN'
    account_nodes = p.nodes_by_id(ui, 'txt_account')
    if len(account_nodes) != 1 or account_nodes[0].get('text', '').strip() != EXPECTED_USER:
        print('RESULT=STOP_USERNAME_INPUT_NOT_CONFIRMED', flush=True)
        return None, 'UNKNOWN'
    LOGIN_USERNAME_VERIFIED_THIS_RUN = True
    password_nodes = p.nodes_by_id(ui, 'txt_pwd')
    if len(password_nodes) != 1:
        print('RESULT=STOP_PASSWORD_FIELD_NOT_UNIQUE', flush=True)
        return None, 'UNKNOWN'
    if password_nodes[0].get('text', '').strip():
        print('RESULT=STOP_PASSWORD_FIELD_NOT_EMPTY', flush=True)
        return None, 'UNKNOWN'

    p.node_tap(password_nodes[0])
    p.send_text(PASSWORD)
    p.adb('shell', 'input', 'keyevent', '4')
    ui = p.read_ui(timeout=APP_UI_COMMAND_TIMEOUT)
    if classify_account_ui(ui) != 'LOGIN_FORM':
        print('RESULT=STOP_LOGIN_FORM_CHANGED_BEFORE_SUBMIT', flush=True)
        return None, 'UNKNOWN'
    account_nodes = p.nodes_by_id(ui, 'txt_account')
    if len(account_nodes) != 1 or account_nodes[0].get('text', '').strip() != EXPECTED_USER:
        print('RESULT=STOP_USERNAME_INPUT_NOT_CONFIRMED_BEFORE_SUBMIT', flush=True)
        return None, 'UNKNOWN'
    confirm_nodes = p.nodes_by_id(ui, 'btn_confirm')
    if len(confirm_nodes) != 1:
        print('RESULT=STOP_LOGIN_CONFIRM_NOT_UNIQUE', flush=True)
        return None, 'UNKNOWN'
    p.node_tap(confirm_nodes[0])
    ui, state = wait_for_account_ui()
    if state in ('AUTHENTICATED_OTHER_ACCOUNT', 'OTHER_EMAIL_VISIBLE'):
        print('RESULT=STOP_OTHER_ACCOUNT_DETECTED_AFTER_LOGIN', flush=True)
        return None, state
    if state == 'AUTHENTICATED_EXPECTED':
        EXPECTED_ACCOUNT_VERIFIED_THIS_RUN = True
    elif (
        state == 'AUTHENTICATED_IDENTITY_UNVERIFIED'
        and LOGIN_USERNAME_VERIFIED_THIS_RUN
        and account_screen(ui)
        and account_identity_status(ui) == 'UNVERIFIED'
    ):
        EXPECTED_ACCOUNT_VERIFIED_THIS_RUN = True
        state = 'AUTHENTICATED_EXPECTED'
    else:
        print('RESULT=STOP_EXPECTED_ACCOUNT_IDENTITY_NOT_VERIFIED', flush=True)
        return None, state
    if not authenticated_account_screen(ui):
        print('RESULT=STOP_EXPECTED_ACCOUNT_IDENTITY_NOT_VERIFIED', flush=True)
        return None, state
    return ui, state


def main():
    global USERNAME, PASSWORD, AUTHENTICATED, EXPECTED_ACCOUNT_VERIFIED_THIS_RUN
    global LOGIN_USERNAME_VERIFIED_THIS_RUN, INITIAL_STATUS_AVAILABLE, IDENTITY_BLOCKED
    global CHECKIN_MODE, SUMMARY_MODE, SUMMARY_IDENTITY_STATUS, SUMMARY_TODAY_STATUS
    global ENTRY_CLICK_ATTEMPTED, CONFIRM_CLICK_ATTEMPTED, SUMMARY_RESULT_CODE
    AUTHENTICATED = False
    EXPECTED_ACCOUNT_VERIFIED_THIS_RUN = False
    LOGIN_USERNAME_VERIFIED_THIS_RUN = False
    INITIAL_STATUS_AVAILABLE = False
    IDENTITY_BLOCKED = False
    ATTEMPTED_SERVERS.clear()
    CHECKIN_MODE = 'checkin'
    SUMMARY_MODE = 'checkin'
    SUMMARY_IDENTITY_STATUS = 'UNKNOWN'
    SUMMARY_TODAY_STATUS = 'NOT_READ'
    SUMMARY_ATTEMPTED_ROUTES.clear()
    ENTRY_CLICK_ATTEMPTED = False
    CONFIRM_CLICK_ATTEMPTED = False
    SUMMARY_RESULT_CODE = 'NOT_STARTED'
    raw_mode = os.environ.get('CHECKIN_MODE', 'checkin')
    if raw_mode not in ('checkin', 'verify'):
        CHECKIN_MODE = 'invalid'
        SUMMARY_MODE = 'invalid'
        SUMMARY_RESULT_CODE = 'STOP_INVALID_MODE'
        print('RESULT=STOP_INVALID_MODE_NO_CLICK', flush=True)
        return 1
    CHECKIN_MODE = raw_mode
    SUMMARY_MODE = raw_mode
    USERNAME = os.environ.get('NCAT_USERNAME', '')
    PASSWORD = os.environ.get('NCAT_PASSWORD', '')
    if USERNAME != EXPECTED_USER or not PASSWORD:
        SUMMARY_RESULT_CODE = 'STOP_ACCOUNT_CONFIG'
        print('RESULT=STOP_ACCOUNT_MISMATCH_OR_MISSING_SECRET')
        return 1

    ui, state = start_app_and_wait_for_account_ui()
    if state in ('LOGIN_FORM', 'LOGGED_OUT'):
        ui, state = login_expected_account(ui, state)
        if ui is None:
            SUMMARY_IDENTITY_STATUS = identity_summary_code(state)
            SUMMARY_RESULT_CODE = 'STOP_IDENTITY_UNVERIFIED'
            return 1
    if state != 'AUTHENTICATED_EXPECTED' or not authenticated_account_screen(ui):
        SUMMARY_IDENTITY_STATUS = identity_summary_code(state)
        if state in ('AUTHENTICATED_OTHER_ACCOUNT', 'AUTHENTICATED_IDENTITY_UNVERIFIED', 'OTHER_EMAIL_VISIBLE'):
            SUMMARY_RESULT_CODE = 'STOP_IDENTITY_UNVERIFIED'
            print('RESULT=STOP_ACCOUNT_IDENTITY_UNVERIFIED_NO_CHECKIN')
        else:
            if state == 'UNKNOWN':
                capture_private_screenshot('00-app-ui-unknown')
            SUMMARY_RESULT_CODE = 'STOP_APP_UI_UNKNOWN'
            print('RESULT=STOP_APP_UI_UNKNOWN_NO_CHECKIN')
        return 1
    AUTHENTICATED = True
    EXPECTED_ACCOUNT_VERIFIED_THIS_RUN = True
    SUMMARY_IDENTITY_STATUS = 'VERIFIED_EXPECTED'

    # Only the exact expected bot identity reaches this first read-only status check.
    if CHECKIN_MODE == 'checkin':
        evidence('01-initial', ui)
    initial_status = account_status(ui)
    SUMMARY_TODAY_STATUS = initial_status if initial_status in ('SIGNED_IN', 'AVAILABLE') else 'UNKNOWN'
    if initial_status == 'SIGNED_IN':
        SUMMARY_RESULT_CODE = 'ALREADY_CHECKED_IN'
        print('INITIAL_STATUS=已签到', flush=True)
        print('RESULT=ALREADY_CHECKED_IN', flush=True)
        return 0
    if initial_status != 'AVAILABLE':
        SUMMARY_RESULT_CODE = 'STOP_STATUS_UNKNOWN'
        print('INITIAL_STATUS=未知', flush=True)
        print('RESULT=STOP_INITIAL_STATUS_UNKNOWN_NO_CLICK', flush=True)
        return 1
    print('INITIAL_STATUS=今日签到', flush=True)
    INITIAL_STATUS_AVAILABLE = True
    if CHECKIN_MODE == 'verify':
        SUMMARY_RESULT_CODE = 'VERIFY_STATUS_AVAILABLE'
        print('RESULT=VERIFY_STATUS_AVAILABLE', flush=True)
        return 0
    SUMMARY_RESULT_CODE = 'CHECKIN_IN_PROGRESS'

    settings = open_settings_from_account()
    if settings is None:
        print('RESULT=STOP_CURRENT_LINE_NOT_READABLE', flush=True)
        return 1
    current_server = p.node_by_id(settings, 'txt_line').get('text', '').strip()
    if current_server not in ALLOWED_SERVERS:
        print('RESULT=STOP_UNSUPPORTED_CURRENT_LINE', flush=True)
        return 1
    print('CURRENT_LINE=' + current_server, flush=True)
    ui = go_back_to_account()
    if ui is None:
        print('RESULT=STOP_ACCOUNT_SCREEN_NOT_REACHED', flush=True)
        return 1

    for index, server in enumerate(server_order(current_server)):
        if server in ATTEMPTED_SERVERS:
            print('RESULT=STOP_DUPLICATE_LINE_BLOCKED', flush=True)
            return 1
        if index == 0:
            line_ui = ui
        else:
            switch_result, line_ui = switch_server(server)
            if switch_result == 'NETWORK_IO':
                # No check-in tap occurred on this line; move on once, then read status there.
                print('NETWORK_IO_DURING_LINE_SWITCH=' + server, flush=True)
                ui = read_authenticated_account_screen()
                if ui is None:
                    print('RESULT=STOP_ACCOUNT_SCREEN_NOT_REACHED_AFTER_LINE_ERROR', flush=True)
                    return 1
                continue
            if switch_result != 'OK' or line_ui is None:
                print('RESULT=STOP_LINE_SWITCH_UNVERIFIED', flush=True)
                return 1
            ui = line_ui

        if not AUTHENTICATED or not authenticated_account_screen(line_ui):
            print('RESULT=STOP_EXPECTED_ACCOUNT_NOT_VERIFIED_NO_CLICK', flush=True)
            return 1
        # Read-only status is checked on each distinct line before any check-in tap.
        status = account_status(line_ui)
        SUMMARY_TODAY_STATUS = status if status in ('SIGNED_IN', 'AVAILABLE') else 'UNKNOWN'
        if status == 'SIGNED_IN':
            SUMMARY_RESULT_CODE = 'ALREADY_CHECKED_IN'
            evidence('04-already-signed-' + server, line_ui)
            print('PERSISTENT_STATUS=已签到', flush=True)
            print('RESULT=ALREADY_CHECKED_IN', flush=True)
            return 0
        if status != 'AVAILABLE':
            if network_io_error(line_ui):
                print('NETWORK_IO_BEFORE_TAP=' + server, flush=True)
                continue
            print('RESULT=STOP_LINE_STATUS_NOT_READABLE', flush=True)
            return 1
        print('READ_ONLY_PRECHECK=' + server + ':今日签到', flush=True)
        result = attempt_on_server(server, line_ui)
        if result == 0:
            return 0
        if result == 'RETRY':
            ui = read_authenticated_account_screen()
            if ui is None:
                print('RESULT=STOP_ACCOUNT_SCREEN_NOT_REACHED_AFTER_NETWORK_IO', flush=True)
                return 1
            continue
        return 1

    print('RESULT=STOP_ALL_LINES_EXHAUSTED_NETWORK_IO', flush=True)
    print('PERSISTENT_STATUS=未确认', flush=True)
    SUMMARY_TODAY_STATUS = 'UNKNOWN'
    SUMMARY_RESULT_CODE = 'STOP_NETWORK_IO_EXHAUSTED'
    return 1


def run():
    global SUMMARY_RESULT_CODE
    exit_code = 1
    try:
        exit_code = main()
    except Exception as exc:
        SUMMARY_RESULT_CODE = 'STOP_ERROR'
        print('RESULT=STOP_ERROR_' + type(exc).__name__)
        if isinstance(exc, RuntimeError):
            print('REASON=' + scrub(str(exc)))
        if AUTHENTICATED and CHECKIN_MODE == 'checkin':
            try:
                evidence('99-error')
            except Exception:
                pass
        exit_code = 1
    finally:
        if SUMMARY_RESULT_CODE == 'CHECKIN_IN_PROGRESS':
            SUMMARY_RESULT_CODE = 'CHECKIN_COMPLETE' if exit_code == 0 else 'CHECKIN_STOP'
        write_step_summary()
    return exit_code


if __name__ == '__main__':
    sys.exit(run())
