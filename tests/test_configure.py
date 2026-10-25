import ast
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from werkzeug.security import check_password_hash
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import configure


class PasswordChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'config.py'
        self.original="SECRET_KEY = 'old-test-key'\nLOGIN_USERNAME = 'unchanged-user'\nLOGIN_PASSWORD_HASH = 'old-test-hash'\nADMIN_PASSWORD_HASH = 'old-report-hash'\nOUTSTANDING_PASSWORD_REQUIRED = False\n"
        self.path.write_text(self.original)

    def values(self):
        return {node.targets[0].id:ast.literal_eval(node.value) for node in ast.parse(self.path.read_text()).body}

    def test_staff_only_preserves_username_reports_password_and_local_setting(self):
        with patch.dict(os.environ,{},clear=True),patch.object(configure,'getpass',side_effect=['new-test-staff','new-test-staff']) as prompt,redirect_stdout(io.StringIO()):
            configure.change_passwords(self.path,staff_only=True)
        values=self.values()
        self.assertEqual(prompt.call_count,2)
        self.assertEqual(values['LOGIN_USERNAME'],'unchanged-user')
        self.assertEqual(values['ADMIN_PASSWORD_HASH'],'old-report-hash')
        self.assertFalse(values['OUTSTANDING_PASSWORD_REQUIRED'])
        self.assertTrue(check_password_hash(values['LOGIN_PASSWORD_HASH'],'new-test-staff'))
        self.assertNotEqual(values['SECRET_KEY'],'old-test-key')
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        self.assertNotIn('new-test-staff',self.path.read_text())

    def test_mismatch_leaves_settings_unchanged(self):
        with patch.dict(os.environ,{},clear=True),patch.object(configure,'getpass',side_effect=['new-test-staff','wrong-confirmation']):
            with self.assertRaises(SystemExit):configure.change_passwords(self.path,staff_only=True)
        self.assertEqual(self.path.read_text(),self.original)

    def test_both_passwords_mode_still_works(self):
        with patch.dict(os.environ,{},clear=True),patch.object(configure,'getpass',side_effect=['new-test-staff','new-test-staff','new-test-report','new-test-report']),redirect_stdout(io.StringIO()):
            configure.change_passwords(self.path)
        values=self.values()
        self.assertTrue(check_password_hash(values['LOGIN_PASSWORD_HASH'],'new-test-staff'))
        self.assertTrue(check_password_hash(values['ADMIN_PASSWORD_HASH'],'new-test-report'))
        self.assertEqual(values['LOGIN_USERNAME'],'unchanged-user')

    def test_environment_override_prevents_misleading_local_change(self):
        with patch.dict(os.environ,{'FT_LOGIN_PASSWORD_HASH':'external-test-hash'},clear=True):
            with self.assertRaises(SystemExit):configure.change_passwords(self.path,staff_only=True)
        self.assertEqual(self.path.read_text(),self.original)


if __name__=='__main__':unittest.main()
