"""Optional Cloudflare: real Compose selection and offline bootstrap/smoke guards.

No daemon mutation, provider request or real .env read.
"""
from __future__ import annotations
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import bootstrap
import smoke
from host_preflight_fixtures import Tree

TOKEN = 'synthetic-cloudflare-token-never-print'
CORE = {'nginx', 'edu_ai_api', 'edu_ai', 'bifrost', 'postgres', 'pgadmin',
        'edu_ai_search', 'edu_ai_web_chat', 'edu_ai_qa_admin'}

def proc(out='', rc=0, err=''):
    return subprocess.CompletedProcess([], rc, out, err)

def model(enabled=False, token=TOKEN):
    services = {'nginx': {'image': 'synthetic-nginx'}}
    if enabled:
        services['cloudflared'] = {'environment': {'TUNNEL_TOKEN': token}}
    return {'services': services}

class BootstrapTests(unittest.TestCase):
    def setUp(self):
        for name in ('_failed', '_needs_recreate'):
            self.addCleanup(setattr, bootstrap, name, getattr(bootstrap, name))
        bootstrap._failed = bootstrap._needs_recreate = False

    def check(self, payload=None, response=None):
        out = io.StringIO()
        with mock.patch.object(bootstrap, 'run', return_value=response or proc(json.dumps(payload))), \
             mock.patch.object(bootstrap, 'read_env', return_value={}), \
             contextlib.redirect_stdout(out):
            result = bootstrap._check_optional_cloudflare()
        self.assertNotIn(TOKEN, out.getvalue())
        return result, out.getvalue()

    def test_disabled_needs_no_token(self):
        result, output = self.check(model())
        self.assertTrue(result)
        self.assertFalse(bootstrap._failed)
        self.assertIn('disabled', output)

    def test_enabled_requires_non_placeholder_token(self):
        for token in (None, '', '   ', 'CHANGE_ME_cloudflare', 123, False):
            with self.subTest(token=token):
                bootstrap._failed = False
                result, output = self.check(model(True, token))
                self.assertFalse(result)
                self.assertTrue(bootstrap._failed)
                self.assertIn('without a usable token', output)

    def test_enabled_valid_token_is_not_printed(self):
        result, output = self.check(model(True))
        self.assertTrue(result)
        self.assertIn('explicitly enabled', output)

    def test_config_failure_never_exposes_body(self):
        for response in (proc(TOKEN, 1, TOKEN), proc(TOKEN), proc('{}'),
                         proc('{"services":[]}'), proc('{"services":{}}'),
                         proc(json.dumps({'services': {'cloudflared': {}}}))):
            with self.subTest(response=response.returncode):
                bootstrap._failed = False
                result, output = self.check(response=response)
                self.assertFalse(result)
                self.assertIn('could not verify', output)

    def test_timeout_stops_without_service_start(self):
        with mock.patch.object(bootstrap, 'run', side_effect=subprocess.TimeoutExpired(['config'], 1)), \
             mock.patch.object(bootstrap, 'compose') as up, \
             mock.patch.object(bootstrap, 'read_env', return_value={}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(bootstrap.step_up([], 'synthetic start'))
        up.assert_not_called()

    def test_missing_token_stops_before_first_up(self):
        with mock.patch.object(bootstrap, 'run', return_value=proc(json.dumps(model(True, '')))), \
             mock.patch.object(bootstrap, 'compose') as up, \
             mock.patch.object(bootstrap, 'read_env', return_value={}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(bootstrap.step_up(['postgres', 'edu_ai'], 'synthetic start'))
        up.assert_not_called()

    def test_local_start_keeps_wait_build_and_recreate(self):
        bootstrap._needs_recreate = True
        with mock.patch.object(bootstrap, 'run', return_value=proc(json.dumps(model()))), \
             mock.patch.object(bootstrap, 'compose', return_value=proc()) as up, \
             mock.patch.object(bootstrap, 'read_env', return_value={}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(bootstrap.step_up([], 'synthetic start', build=True, apply_pending_recreate=True))
        args = up.call_args.args
        self.assertIn('--wait', args)
        self.assertIn('--build', args)
        self.assertIn('--force-recreate', args)

class SmokeTests(unittest.TestCase):
    def setUp(self):
        for name in ('_passed', '_failed', '_skipped'):
            self.addCleanup(setattr, smoke, name, getattr(smoke, name))
            setattr(smoke, name, 0)

    def check(self, expected, rows, selected=None):
        selected = selected or proc('\n'.join(sorted(expected)))
        replies = [selected, proc(json.dumps(rows))]
        out = io.StringIO()
        with mock.patch.object(smoke, 'run', side_effect=replies) as run, contextlib.redirect_stdout(out):
            smoke.check_containers()
        if len(run.call_args_list) == 2:
            self.assertIn('--all', run.call_args_list[-1].args[0])
        return out.getvalue()

    @staticmethod
    def row(name, state='running', health='healthy'):
        return {'Service': name, 'State': state, 'Health': health}

    def test_local_stack_passes_without_tunnel(self):
        output = self.check(CORE, [self.row(name) for name in CORE])
        self.assertEqual(smoke._failed, 0)
        self.assertEqual(smoke._passed, 9)
        self.assertEqual(smoke._skipped, 1)
        self.assertIn('Cloudflare Tunnel disabled', output)

    def test_enabled_running_tunnel_is_checked(self):
        names = CORE | {'cloudflared'}
        self.check(names, [self.row(name) for name in names])
        self.assertEqual(smoke._passed, 10)
        self.assertEqual(smoke._failed + smoke._skipped, 0)

    def test_missing_enabled_tunnel_fails(self):
        output = self.check(CORE | {'cloudflared'}, [self.row(name) for name in CORE])
        self.assertEqual(smoke._failed, 1)
        self.assertIn('cloudflared: enabled service has no container', output)

    def test_exited_or_unhealthy_enabled_tunnel_fails(self):
        for state, health in (('exited', ''), ('running', 'unhealthy')):
            with self.subTest(state=state):
                smoke._failed = 0
                output = self.check(CORE | {'cloudflared'},
                                    [self.row(name) for name in CORE] + [self.row('cloudflared', state, health)])
                self.assertEqual(smoke._failed, 1)
                self.assertIn('cloudflared', output)

    def test_missing_core_is_not_hidden_by_optional_tunnel(self):
        output = self.check(CORE, [self.row(name) for name in CORE - {'postgres'}])
        self.assertEqual(smoke._failed, 1)
        self.assertIn('postgres: enabled service has no container', output)

    def test_config_failure_is_not_a_skip(self):
        output = self.check(CORE, [], selected=proc('', 1, TOKEN))
        self.assertEqual(smoke._failed, 1)
        self.assertEqual(smoke._skipped, 0)
        self.assertNotIn(TOKEN, output)

    def test_disabled_profile_does_not_hide_leftover_broken_container(self):
        self.check(CORE, [self.row(name) for name in CORE] + [self.row('cloudflared', 'exited', '')])
        self.assertEqual(smoke._failed, 1)

class PreflightTests(Tree):
    def test_optional_connector_absence_needs_no_cloudflare_account(self):
        self.world.model['services'].pop('cloudflared')
        self.env_values.pop('CLOUDFLARE_TUNNEL_TOKEN', None)
        self.write_set()
        report = self.check()
        self.assertEqual(report.exit_code(), 0, '\n'.join(report.lines()))
        self.assertFalse(any('cloudflare/cloudflared' in str(call) for call in self.host.calls))

@unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI unavailable')
class ComposeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='eduai_cf_selection_')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # All source/config paths are synthetic; never read the real installation .env.
        shutil.copyfile(ROOT / 'docker-compose.yml', self.root / 'docker-compose.yml')
        shutil.copyfile(ROOT / 'docker-compose.override.yml', self.root / 'docker-compose.override.yml')
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('COMPOSE_', 'DOCKER_', 'EDU_AI_'))}
        self.base = 'EDU_AI_TUNNEL_SUBNET=10.231.250.0/29\nPOSTGRES_PASSWORD=synthetic-postgres\n'
        self.envpath = self.root / '.env'
        self.envpath.write_text(self.base, encoding='utf-8')
        self.prefix = ['docker', 'compose', '-p', 'eduai_cf_selection', '--project-directory', str(self.root),
                       '--env-file', str(self.envpath), '-f', str(self.root / 'docker-compose.yml')]

    def render(self, *extra):
        result = subprocess.run([*self.prefix, *extra, 'config', '--format', 'json'],
                                env=self.env, cwd=self.root, text=True, encoding='utf-8',
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, 'synthetic Compose config failed')
        return json.loads(result.stdout)

    def test_default_excludes_tunnel_image_and_egress(self):
        data = self.render()
        self.assertEqual(set(data['services']), CORE)
        self.assertNotIn('tunnel_egress', data['networks'])
        self.assertTrue(data['networks']['tunnel']['internal'])
        self.assertIn('set_real_ip_from', data['configs']['tunnel_trust']['content'])

    def test_token_alone_does_not_enable_tunnel(self):
        self.envpath.write_text(self.base + f'CLOUDFLARE_TUNNEL_TOKEN={TOKEN}\n', encoding='utf-8')
        self.assertEqual(set(self.render()['services']), CORE)

    def test_external_env_profile_explicitly_enables_tunnel(self):
        self.envpath.write_text(self.base + f'COMPOSE_PROFILES=cloudflare\nCLOUDFLARE_TUNNEL_TOKEN={TOKEN}\n', encoding='utf-8')
        data = self.render()
        self.assertEqual(set(data['services']), CORE | {'cloudflared'})
        self.assertEqual(data['services']['cloudflared']['environment']['TUNNEL_TOKEN'], TOKEN)
        self.assertEqual(set(data['services']['cloudflared']['networks']), {'app', 'tunnel', 'tunnel_egress'})
        self.assertTrue(data['networks']['tunnel']['internal'])
        self.assertNotIn('ports', data['services']['cloudflared'])

    def test_enabling_profile_preserves_every_core_service(self):
        before = self.render()
        after = self.render('--profile', 'cloudflare')
        for name in CORE:
            self.assertEqual(before['services'][name], after['services'][name], name)
        self.assertEqual(before['configs'], after['configs'])
        self.assertEqual(before['volumes'], after['volumes'])

    def test_dev_override_does_not_enable_cloudflare(self):
        self.assertEqual(set(self.render('-f', str(self.root / 'docker-compose.override.yml'))['services']), CORE)

if __name__ == '__main__':
    unittest.main()
