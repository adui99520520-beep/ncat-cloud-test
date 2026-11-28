"""Offline, non-interactive safety tests for check-in identity and UI readiness."""
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape


REPO_ROOT = Path(__file__).resolve().parents[1]
os.chdir(REPO_ROOT)
_EVIDENCE_TMP = tempfile.TemporaryDirectory(prefix='ncat-checkin-tests-')
os.environ['EVIDENCE_DIR'] = _EVIDENCE_TMP.name
_spec = importlib.util.spec_from_file_location('checkin_evidence_under_test', REPO_ROOT / 'scripts/checkin-evidence.py')
checkin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checkin)
probe = checkin.p


def xml_node(resource_id, text='', extra=''):
    return f'<node resource-id="app:id/{resource_id}" text="{escape(text)}" {extra}/>'


def account_page(user_label, status='今日签到', extra=''):
    return ET.fromstring(
        '<hierarchy>'
        + xml_node('txt_user', user_label)
        + xml_node('txt_accelerate', status)
        + extra
        + '</hierarchy>'
    )


def login_page(account='', extra=''):
    return ET.fromstring(
        '<hierarchy>'
        + xml_node('txt_account', account)
        + xml_node('txt_pwd', '')
        + xml_node('btn_confirm', '登录', 'enabled="true" visible-to-user="true" bounds="[0,0][20,20]"')
        + extra
        + '</hierarchy>'
    )


class CheckinIdentityTests(unittest.TestCase):
    def setUp(self):
        checkin.USERNAME = checkin.EXPECTED_USER
        checkin.PASSWORD = 'mock-only-not-a-real-secret'
        checkin.AUTHENTICATED = False
        checkin.EXPECTED_ACCOUNT_VERIFIED_THIS_RUN = False
        checkin.LOGIN_USERNAME_VERIFIED_THIS_RUN = False
        checkin.INITIAL_STATUS_AVAILABLE = False
        checkin.ATTEMPTED_SERVERS.clear()
        checkin.IDENTITY_BLOCKED = False
        checkin.CHECKIN_MODE = 'checkin'
        checkin.SUMMARY_MODE = 'checkin'
        checkin.SUMMARY_IDENTITY_STATUS = 'UNKNOWN'
        checkin.SUMMARY_TODAY_STATUS = 'NOT_READ'
        checkin.SUMMARY_ATTEMPTED_ROUTES.clear()
        checkin.ENTRY_CLICK_ATTEMPTED = False
        checkin.CONFIRM_CLICK_ATTEMPTED = False
        checkin.SUMMARY_RESULT_CODE = 'NOT_STARTED'

    def run_with_mock_app(self, mode, ui, state, summary_name):
        summary = Path(_EVIDENCE_TMP.name) / summary_name
        summary.write_text('', encoding='utf-8')
        env = {
            'NCAT_USERNAME': checkin.EXPECTED_USER,
            'NCAT_PASSWORD': 'mock-only-not-a-real-secret',
            'GITHUB_STEP_SUMMARY': str(summary),
        }
        if mode is not None:
            env['CHECKIN_MODE'] = mode
        with patch.dict(os.environ, env, clear=True), \
             patch.object(probe, 'adb') as adb, \
             patch.object(probe, 'node_tap') as tap, \
             patch.object(checkin, 'wait_for_account_ui', return_value=(ui, state)) as wait_ui, \
             patch.object(checkin, 'open_settings_from_account') as settings, \
             patch.object(checkin, 'evidence') as evidence, \
             patch.object(checkin, 'capture_private_screenshot') as private_screenshot, \
             patch.object(checkin.time, 'sleep'):
            with contextlib.redirect_stdout(io.StringIO()):
                result = checkin.run()
            calls = {
                'adb': adb.call_count,
                'tap': tap.call_count,
                'wait': wait_ui.call_count,
                'settings': settings.call_count,
                'evidence': evidence.call_count,
                'private_screenshot': private_screenshot.call_count,
            }
        return result, summary.read_text(encoding='utf-8').splitlines(), calls

    def test_existing_session_needs_visible_email_or_stops(self):
        expected = account_page(checkin.EXPECTED_USER)
        nickname_only = account_page('文艺的雪糕')
        other_email = account_page('昵称', extra=xml_node('label_email', 'other@example.invalid'))
        mixed_emails = account_page(checkin.EXPECTED_USER, extra=xml_node('label_email', 'other@example.invalid'))

        self.assertEqual(checkin.classify_account_ui(expected), 'AUTHENTICATED_EXPECTED')
        self.assertTrue(checkin.authenticated_account_screen(expected))
        self.assertEqual(checkin.classify_account_ui(nickname_only), 'AUTHENTICATED_IDENTITY_UNVERIFIED')
        self.assertEqual(checkin.classify_account_ui(other_email), 'AUTHENTICATED_OTHER_ACCOUNT')
        self.assertEqual(checkin.classify_account_ui(mixed_emails), 'AUTHENTICATED_OTHER_ACCOUNT')

        checkin.AUTHENTICATED = True
        checkin.INITIAL_STATUS_AVAILABLE = True
        with patch.object(probe, 'node_tap') as tap, patch.object(probe, 'adb') as adb:
            with contextlib.redirect_stdout(io.StringIO()):
                result = checkin.attempt_on_server('精品3', nickname_only)
            self.assertEqual(result, 1)
            tap.assert_not_called()
            adb.assert_not_called()

    def test_fresh_login_input_can_bind_a_nickname_only_page_for_this_run(self):
        form = login_page()
        form_with_expected = login_page(checkin.EXPECTED_USER)
        nickname_page = account_page('文艺的雪糕')
        with patch.object(probe, 'node_tap') as tap, \
             patch.object(probe, 'send_text') as send_text, \
             patch.object(probe, 'adb') as adb, \
             patch.object(probe, 'read_ui', side_effect=[form_with_expected, form_with_expected, nickname_page]):
            ui, state = checkin.login_expected_account(form, 'LOGIN_FORM')

        self.assertIs(ui, nickname_page)
        self.assertEqual(state, 'AUTHENTICATED_EXPECTED')
        self.assertTrue(checkin.LOGIN_USERNAME_VERIFIED_THIS_RUN)
        self.assertTrue(checkin.EXPECTED_ACCOUNT_VERIFIED_THIS_RUN)
        typed_values = [call.args[0] for call in send_text.call_args_list]
        self.assertTrue(typed_values and typed_values[0] == checkin.EXPECTED_USER)
        self.assertEqual(len(typed_values), 2)
        self.assertTrue(any(call.args[0].get('resource-id', '').endswith(':id/btn_confirm') for call in tap.call_args_list))
        self.assertTrue(adb.called)

        # The verified-in-this-run identity survives a route returning only the nickname,
        # but never overrides a newly visible different email.
        self.assertTrue(checkin.authenticated_account_screen(nickname_page))
        other_email = account_page('文艺的雪糕', extra=xml_node('label_email', 'other@example.invalid'))
        self.assertEqual(checkin.classify_account_ui(other_email), 'AUTHENTICATED_OTHER_ACCOUNT')
        self.assertFalse(checkin.authenticated_account_screen(other_email))

    def test_other_email_after_login_stops_even_after_username_was_confirmed(self):
        form = login_page()
        form_with_expected = login_page(checkin.EXPECTED_USER)
        other_page = account_page('文艺的雪糕', extra=xml_node('label_email', 'other@example.invalid'))
        with patch.object(probe, 'node_tap'), \
             patch.object(probe, 'send_text'), \
             patch.object(probe, 'adb'), \
             patch.object(probe, 'read_ui', side_effect=[form_with_expected, form_with_expected, other_page]):
            ui, state = checkin.login_expected_account(form, 'LOGIN_FORM')

        self.assertIsNone(ui)
        self.assertEqual(state, 'AUTHENTICATED_OTHER_ACCOUNT')
        self.assertTrue(checkin.LOGIN_USERNAME_VERIFIED_THIS_RUN)
        self.assertFalse(checkin.EXPECTED_ACCOUNT_VERIFIED_THIS_RUN)

    def test_other_email_blocks_network_error_route_retry(self):
        other_page = account_page('昵称', extra=xml_node('label_email', 'other@example.invalid'))
        self.assertTrue(checkin.block_on_other_email(other_page))
        with patch.object(checkin, 'network_io_error') as network_error, \
             patch.object(checkin, 'persistent_status_after_attempt') as persistent_status:
            with contextlib.redirect_stdout(io.StringIO()):
                result = checkin.retry_decision_after_network_error('精品3', other_page)
        self.assertEqual(result, 1)
        network_error.assert_not_called()
        persistent_status.assert_not_called()

    def test_prefilled_or_mismatched_username_never_submits(self):
        prefilled = login_page(checkin.EXPECTED_USER)
        with patch.object(probe, 'node_tap') as tap, patch.object(probe, 'send_text') as send_text:
            with contextlib.redirect_stdout(io.StringIO()):
                ui, _ = checkin.login_expected_account(prefilled, 'LOGIN_FORM')
            self.assertIsNone(ui)
            send_text.assert_not_called()
            self.assertFalse(any(call.args[0].get('resource-id', '').endswith(':id/btn_confirm') for call in tap.call_args_list))
            self.assertFalse(checkin.LOGIN_USERNAME_VERIFIED_THIS_RUN)

        form = login_page()
        wrong_readback = login_page('wrong@example.invalid')
        with patch.object(probe, 'node_tap') as tap, \
             patch.object(probe, 'send_text'), \
             patch.object(probe, 'read_ui', return_value=wrong_readback):
            with contextlib.redirect_stdout(io.StringIO()):
                ui, _ = checkin.login_expected_account(form, 'LOGIN_FORM')
            self.assertIsNone(ui)
            self.assertFalse(checkin.LOGIN_USERNAME_VERIFIED_THIS_RUN)
            self.assertFalse(any(call.args[0].get('resource-id', '').endswith(':id/btn_confirm') for call in tap.call_args_list))

        other_email_on_login_page = login_page(extra=xml_node('label_email', 'other@example.invalid'))
        self.assertEqual(checkin.classify_account_ui(other_email_on_login_page), 'OTHER_EMAIL_VISIBLE')
        with patch.object(probe, 'node_tap') as tap, patch.object(probe, 'send_text') as send_text:
            with contextlib.redirect_stdout(io.StringIO()):
                ui, _ = checkin.login_expected_account(other_email_on_login_page, 'OTHER_EMAIL_VISIBLE')
            self.assertIsNone(ui)
            send_text.assert_not_called()
            self.assertFalse(any(call.args[0].get('resource-id', '').endswith(':id/btn_confirm') for call in tap.call_args_list))

    def test_username_is_rechecked_immediately_before_submit(self):
        form = login_page()
        form_with_expected = login_page(checkin.EXPECTED_USER)
        changed_before_submit = login_page('wrong@example.invalid')
        with patch.object(probe, 'node_tap') as tap, \
             patch.object(probe, 'send_text'), \
             patch.object(probe, 'adb'), \
             patch.object(probe, 'read_ui', side_effect=[form_with_expected, changed_before_submit]):
            with contextlib.redirect_stdout(io.StringIO()):
                ui, _ = checkin.login_expected_account(form, 'LOGIN_FORM')
            self.assertIsNone(ui)
            self.assertFalse(any(call.args[0].get('resource-id', '').endswith(':id/btn_confirm') for call in tap.call_args_list))
            self.assertFalse(checkin.EXPECTED_ACCOUNT_VERIFIED_THIS_RUN)

    def test_verify_mode_reads_status_and_exits_before_any_checkin_control(self):
        raw_ui = 'RAW_PAGE_TEXT password=mock-only-not-a-real-secret token=mock-token code=123456'
        expected = account_page(
            checkin.EXPECTED_USER, status='今日签到', extra=xml_node('debug', raw_ui)
        )
        result, summary, calls = self.run_with_mock_app(
            'verify', expected, 'AUTHENTICATED_EXPECTED', 'verify-summary.txt'
        )
        self.assertEqual(result, 0)
        self.assertEqual(calls['tap'], 0)
        self.assertEqual(calls['settings'], 0)
        self.assertEqual(calls['evidence'], 0)
        self.assertEqual(summary, [
            'mode=verify',
            'identity_status=VERIFIED_EXPECTED',
            'today_status=AVAILABLE',
            'attempted_routes=none',
            'entry_click_attempted=no',
            'confirmation_click_attempted=no',
            'result_code=VERIFY_STATUS_AVAILABLE',
        ])
        joined = '\n'.join(summary)
        for forbidden in (
            checkin.EXPECTED_USER, '文艺的雪糕', '今日签到', '点击签到', '登录',
            'RAW_PAGE_TEXT', 'mock-only-not-a-real-secret', 'mock-token', '123456',
        ):
            self.assertNotIn(forbidden, joined)

    def test_verify_mode_already_signed_in_exits_without_route_or_click(self):
        expected = account_page(checkin.EXPECTED_USER, status='已签到')
        result, summary, calls = self.run_with_mock_app(
            'verify', expected, 'AUTHENTICATED_EXPECTED', 'verify-already-signed.txt'
        )
        self.assertEqual(result, 0)
        self.assertEqual(calls['tap'], 0)
        self.assertEqual(calls['settings'], 0)
        self.assertEqual(calls['evidence'], 0)
        self.assertEqual(summary[2], 'today_status=SIGNED_IN')
        self.assertEqual(summary[3], 'attempted_routes=none')
        self.assertEqual(summary[4], 'entry_click_attempted=no')
        self.assertEqual(summary[5], 'confirmation_click_attempted=no')
        self.assertEqual(summary[6], 'result_code=ALREADY_CHECKED_IN')

    def test_verify_mode_unknown_or_other_identity_stops_without_click(self):
        cases = (
            (None, 'UNKNOWN', 'UNKNOWN', 'STOP_APP_UI_UNKNOWN'),
            (account_page('昵称', extra=xml_node('label_email', 'other@example.invalid')),
             'AUTHENTICATED_OTHER_ACCOUNT', 'OTHER_ACCOUNT', 'STOP_IDENTITY_UNVERIFIED'),
        )
        for index, (ui, state, identity_code, result_code) in enumerate(cases):
            with self.subTest(state=state):
                result, summary, calls = self.run_with_mock_app(
                    'verify', ui, state, f'verify-blocked-{index}.txt'
                )
                self.assertEqual(result, 1)
                self.assertEqual(calls['tap'], 0)
                self.assertEqual(calls['settings'], 0)
                self.assertEqual(summary[1], 'identity_status=' + identity_code)
                self.assertEqual(summary[2], 'today_status=NOT_READ')
                self.assertEqual(summary[4], 'entry_click_attempted=no')
                self.assertEqual(summary[5], 'confirmation_click_attempted=no')
                self.assertEqual(summary[6], 'result_code=' + result_code)
                self.assertEqual(calls['private_screenshot'], 1 if state == 'UNKNOWN' else 0)

    def test_verify_mode_unknown_current_day_status_stops_without_click(self):
        expected = account_page(checkin.EXPECTED_USER, status='Loading')
        result, summary, calls = self.run_with_mock_app(
            'verify', expected, 'AUTHENTICATED_EXPECTED', 'verify-unknown-status.txt'
        )
        self.assertEqual(result, 1)
        self.assertEqual(calls['tap'], 0)
        self.assertEqual(calls['settings'], 0)
        self.assertEqual(calls['evidence'], 0)
        self.assertEqual(summary[2], 'today_status=UNKNOWN')
        self.assertEqual(summary[4], 'entry_click_attempted=no')
        self.assertEqual(summary[5], 'confirmation_click_attempted=no')

    def test_invalid_mode_stops_before_app_or_click(self):
        result, summary, calls = self.run_with_mock_app(
            'unsafe-mode', None, 'UNKNOWN', 'invalid-mode-summary.txt'
        )
        self.assertEqual(result, 1)
        self.assertEqual(calls['adb'], 0)
        self.assertEqual(calls['tap'], 0)
        self.assertEqual(calls['wait'], 0)
        self.assertEqual(summary[0], 'mode=invalid')
        self.assertEqual(summary[4], 'entry_click_attempted=no')
        self.assertEqual(summary[5], 'confirmation_click_attempted=no')
        self.assertNotIn('unsafe-mode', '\n'.join(summary))

    def test_checkin_default_retains_identity_gate(self):
        nickname_only = account_page('文艺的雪糕')
        result, summary, calls = self.run_with_mock_app(
            None, nickname_only, 'AUTHENTICATED_IDENTITY_UNVERIFIED', 'checkin-identity-gate.txt'
        )
        self.assertEqual(result, 1)
        self.assertEqual(calls['tap'], 0)
        self.assertEqual(calls['settings'], 0)
        self.assertEqual(summary[0], 'mode=checkin')
        self.assertEqual(summary[1], 'identity_status=UNVERIFIED')
        self.assertEqual(summary[2], 'today_status=NOT_READ')
        self.assertEqual(summary[6], 'result_code=STOP_IDENTITY_UNVERIFIED')

    def test_checkin_mode_stops_on_unknown_status_before_route_or_click(self):
        expected = account_page(checkin.EXPECTED_USER, status='Loading')
        result, summary, calls = self.run_with_mock_app(
            'checkin', expected, 'AUTHENTICATED_EXPECTED', 'checkin-status-gate.txt'
        )
        self.assertEqual(result, 1)
        self.assertEqual(calls['tap'], 0)
        self.assertEqual(calls['settings'], 0)
        self.assertEqual(summary[1], 'identity_status=VERIFIED_EXPECTED')
        self.assertEqual(summary[2], 'today_status=UNKNOWN')
        self.assertEqual(summary[3], 'attempted_routes=none')
        self.assertEqual(summary[4], 'entry_click_attempted=no')
        self.assertEqual(summary[5], 'confirmation_click_attempted=no')

    def test_switching_line_without_entry_tap_is_not_reported_as_attempted(self):
        settings_before = ET.fromstring(
            '<hierarchy><node resource-id="app:id/txt_line" text="精品3"/></hierarchy>'
        )
        picker = ET.fromstring(
            '<hierarchy><node text="选择应用服务器"/>'
            '<node text="精品1" enabled="true" visible-to-user="true" bounds="[10,10][100,50]"/>'
            '</hierarchy>'
        )
        settings_after = ET.fromstring(
            '<hierarchy><node resource-id="app:id/txt_line" text="精品1"/></hierarchy>'
        )
        summary_path = Path(_EVIDENCE_TMP.name) / 'switch-only-summary.txt'
        summary_path.write_text('', encoding='utf-8')

        with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary_path)}), \
             patch.object(checkin, 'open_settings_from_account', return_value=settings_before), \
             patch.object(checkin, 'tap_unique_text', return_value=True), \
             patch.object(probe, 'read_ui', side_effect=[picker, settings_after]), \
             patch.object(checkin, 'go_back_to_account', return_value=account_page(checkin.EXPECTED_USER)), \
             patch.object(probe, 'node_tap') as tap, \
             patch.object(checkin.time, 'sleep'):
            result, _ = checkin.switch_server('精品1')
            checkin.write_step_summary()

        self.assertEqual(result, 'OK')
        tap.assert_called_once()  # server-picker selection only, not the check-in entry
        self.assertEqual(checkin.SUMMARY_ATTEMPTED_ROUTES, [])
        self.assertFalse(checkin.ENTRY_CLICK_ATTEMPTED)
        self.assertIn('attempted_routes=none', summary_path.read_text(encoding='utf-8'))

    def test_checkin_summary_lists_ordered_allowlisted_routes_only_after_entry_taps(self):
        raw_ui = 'RAW_PAGE_TEXT password=mock-only-not-a-real-secret token=mock-token code=123456'
        initial = account_page(
            checkin.EXPECTED_USER, status='今日签到', extra=xml_node('debug', raw_ui)
        )
        checkin.AUTHENTICATED = True
        checkin.EXPECTED_ACCOUNT_VERIFIED_THIS_RUN = True
        checkin.INITIAL_STATUS_AVAILABLE = True
        checkin.SUMMARY_MODE = 'checkin'
        checkin.SUMMARY_IDENTITY_STATUS = 'VERIFIED_EXPECTED'
        checkin.SUMMARY_TODAY_STATUS = 'AVAILABLE'
        checkin.SUMMARY_RESULT_CODE = 'CHECKIN_COMPLETE'
        summary_path = Path(_EVIDENCE_TMP.name) / 'ordered-routes-summary.txt'
        summary_path.write_text('', encoding='utf-8')

        def signed_in(_server):
            checkin.SUMMARY_TODAY_STATUS = 'SIGNED_IN'
            return 'SIGNED_IN', initial

        with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary_path)}), \
             patch.object(probe, 'node_tap') as tap, \
             patch.object(probe, 'read_ui', return_value=initial), \
             patch.object(checkin, 'clear_logcat'), \
             patch.object(checkin, 'evidence'), \
             patch.object(checkin, 'record_countdown'), \
             patch.object(checkin, 'success', side_effect=(False, True, False, True)), \
             patch.object(checkin, 'persistent_status_after_attempt', side_effect=signed_in), \
             patch.object(checkin.time, 'sleep'):
            first = checkin.attempt_on_server('精品3', initial)
            second = checkin.attempt_on_server('精品1', initial)
            checkin.write_step_summary()

        self.assertEqual((first, second), (0, 0))
        self.assertEqual(tap.call_count, 2)
        self.assertEqual(checkin.SUMMARY_ATTEMPTED_ROUTES, ['精品3', '精品1'])
        summary = summary_path.read_text(encoding='utf-8')
        self.assertIn('attempted_routes=精品3,精品1', summary)
        self.assertIn('entry_click_attempted=yes', summary)
        self.assertIn('confirmation_click_attempted=no', summary)
        routes_value = next(line.split('=', 1)[1] for line in summary.splitlines() if line.startswith('attempted_routes='))
        routes = [] if routes_value == 'none' else routes_value.split(',')
        self.assertLessEqual(len(routes), 5)
        self.assertTrue(all(route in checkin.ALLOWED_SERVERS for route in routes))
        for forbidden in (
            checkin.EXPECTED_USER, 'RAW_PAGE_TEXT', 'mock-only-not-a-real-secret',
            'mock-token', '123456', '今日签到', '点击签到',
        ):
            self.assertNotIn(forbidden, summary)

    def test_route_attempt_recorder_rejects_non_allowlisted_and_caps_at_five(self):
        self.assertFalse(checkin.record_route_attempt('RAW_PAGE_TEXT'))
        for server in checkin.ALLOWED_SERVERS:
            self.assertTrue(checkin.record_route_attempt(server))
        self.assertEqual(checkin.SUMMARY_ATTEMPTED_ROUTES, list(checkin.ALLOWED_SERVERS))
        self.assertFalse(checkin.record_route_attempt(checkin.ALLOWED_SERVERS[0]))
        self.assertFalse(checkin.record_route_attempt('another-unlisted-route'))
        self.assertLessEqual(len(checkin.SUMMARY_ATTEMPTED_ROUTES), 5)

    def test_app_start_waits_for_stability_before_ui_read(self):
        events = []

        def adb(*args, **kwargs):
            events.append('launch')

        def sleep(seconds):
            events.append(('sleep', seconds))

        def wait():
            events.append('read')
            return None, 'UNKNOWN'

        with patch.object(probe, 'adb', side_effect=adb), \
             patch.object(checkin.time, 'sleep', side_effect=sleep), \
             patch.object(checkin, 'wait_for_account_ui', side_effect=wait):
            ui, state = checkin.start_app_and_wait_for_account_ui()

        self.assertIsNone(ui)
        self.assertEqual(state, 'UNKNOWN')
        self.assertEqual(events, ['launch', ('sleep', checkin.APP_UI_STARTUP_DELAY), 'read'])
        self.assertEqual(checkin.APP_UI_STARTUP_DELAY, 30)

    def test_ui_readiness_retries_with_bounded_read_timeout(self):
        unknown = ET.fromstring('<hierarchy><node text="Loading"/></hierarchy>')
        login = login_page()
        timeouts = []
        answers = iter((unknown, login))

        def read_ui(timeout=45):
            timeouts.append(timeout)
            return next(answers)

        with patch.object(probe, 'read_ui', side_effect=read_ui), patch.object(checkin.time, 'sleep'):
            ui, state = checkin.wait_for_account_ui(timeout=60)
        self.assertIs(ui, login)
        self.assertEqual(state, 'LOGIN_FORM')
        self.assertTrue(timeouts and all(0 < timeout <= checkin.APP_UI_COMMAND_TIMEOUT for timeout in timeouts))
        self.assertGreater(max(timeouts), 5)
        self.assertEqual(checkin.APP_UI_COMMAND_TIMEOUT, 45)

        with patch.object(probe, 'read_ui') as read_ui_mock:
            ui, state = checkin.wait_for_account_ui(timeout=0)
        self.assertIsNone(ui)
        self.assertEqual(state, 'UNKNOWN')
        read_ui_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
