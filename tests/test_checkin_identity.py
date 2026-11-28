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

    def test_ui_readiness_retries_with_bounded_read_timeout(self):
        unknown = ET.fromstring('<hierarchy><node text="Loading"/></hierarchy>')
        login = login_page()
        timeouts = []
        answers = iter((unknown, login))

        def read_ui(timeout=45):
            timeouts.append(timeout)
            return next(answers)

        with patch.object(probe, 'read_ui', side_effect=read_ui), patch.object(checkin.time, 'sleep'):
            ui, state = checkin.wait_for_account_ui(timeout=5)
        self.assertIs(ui, login)
        self.assertEqual(state, 'LOGIN_FORM')
        self.assertTrue(timeouts and all(0 < timeout <= 5 for timeout in timeouts))

        with patch.object(probe, 'read_ui') as read_ui_mock:
            ui, state = checkin.wait_for_account_ui(timeout=0)
        self.assertIsNone(ui)
        self.assertEqual(state, 'UNKNOWN')
        read_ui_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
