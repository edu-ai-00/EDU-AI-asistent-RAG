"""Offline smoke policy: preserve user variables and enforce platform types. No Docker/DB access."""
import subprocess
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import smoke


class GlobalVariables(unittest.TestCase):
    def check(self, rows):
        output='\n'.join(f'{name}={kind}' for name,kind in rows)+'\n'
        result=subprocess.CompletedProcess([], 0, stdout=output, stderr='')
        with patch.object(smoke,'run',return_value=result) as run, \
             patch.object(smoke,'bad') as bad, patch.object(smoke,'ok') as ok, patch.object(smoke,'skip') as skip:
            smoke.check_global_variables()
            # This check observes names/types only and never changes user data.
            for args in run.call_args_list:
                query=args.args[0][-1]
                self.assertTrue(query.startswith('SELECT '))
                self.assertNotIn('value',query.lower())
            skip.assert_not_called()
            return bad,ok

    def required(self):
        return [('OPENAI_API_KEY','Credential'),('POSTGRES_PASSWORD','Credential')]

    def test_managed_credentials_and_optional_generic_types(self):
        rows=self.required()+[(n,'Generic') for n in smoke.OPTIONAL_GENERIC_VARIABLES]
        bad,ok=self.check(rows)
        bad.assert_not_called();ok.assert_called_once()

    def test_user_generic_and_credential_coexist_without_value_access(self):
        bad,ok=self.check(self.required()+[('USER_GENERIC','Generic'),('USER_CREDENTIAL','Credential')])
        bad.assert_not_called();ok.assert_called_once()
        self.assertIn('user variables preserved: 2',ok.call_args.args[0])
        self.assertNotIn('USER_CREDENTIAL',ok.call_args.args[0])

    def test_each_connection_name_rejects_credential_even_with_user_variables(self):
        for name in smoke.OPTIONAL_GENERIC_VARIABLES:
            with self.subTest(name=name):
                bad,ok=self.check(self.required()+[(name,'Credential'),('USER_EXTRA','Generic')])
                bad.assert_called_once();ok.assert_not_called()
                self.assertIn('wrong type',bad.call_args.args[0])

    def test_each_required_credential_rejects_wrong_type(self):
        for name in smoke.EXPECTED_VARIABLES:
            with self.subTest(name=name):
                rows=[(n,'Generic' if n==name else kind) for n,kind in self.required()]
                bad,ok=self.check(rows)
                bad.assert_called_once();ok.assert_not_called()

    def test_missing_required_credential_is_still_a_failure(self):
        for name in smoke.EXPECTED_VARIABLES:
            with self.subTest(name=name):
                rows=[(n,kind) for n,kind in self.required() if n!=name]+[('USER_EXTRA','Credential')]
                bad,ok=self.check(rows)
                bad.assert_called_once();ok.assert_not_called()
                self.assertIn('missing',bad.call_args.args[0])

    def test_optional_generic_names_may_use_environment_fallback(self):
        bad,ok=self.check(self.required())
        bad.assert_not_called();ok.assert_called_once()


if __name__=='__main__':
    unittest.main()
