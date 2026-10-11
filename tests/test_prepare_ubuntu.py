"""Offline host preparation guards; all system mutations are mocked. No apt, sudo or Docker daemon access."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = (str(Path('C:/Program Files/Git/bin/bash.exe')) if os.name == 'nt' else shutil.which('bash'))


@unittest.skipUnless(BASH and Path(BASH).exists(), 'Bash is required')
class PrepareUbuntu(unittest.TestCase):
    def run_mock(self, arguments='--apply --docker-user lab', *, conflict='', containers='0', apt_fails=False, sudo_fails=False, fresh=False, extra=''):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'calls'
            env = dict(os.environ, PREP_LOG=log.as_posix(), PREP_CONFLICT=conflict,
                       PREP_CONTAINERS=containers, PREP_APT_FAIL='1' if apt_fails else '0',
                       PREP_SUDO_FAIL='1' if sudo_fails else '0', PREP_FRESH='1' if fresh else '0')
            env.pop('DOCKER_HOST', None)
            env.pop('DOCKER_CONTEXT', None)
            # Pretend an existing official CE installation is present. Every
            # privileged operation is intercepted, including when run as root.
            code = r'''
source scripts/prepare_ubuntu.sh
command() {
    if [[ $PREP_FRESH == 1 && $* == '-v docker' ]]; then return 1; fi
    builtin command "$@"
}
read_platform() { ID=ubuntu; VERSION_ID=26.04; VERSION_CODENAME=resolute; }
uname() { printf x86_64; }
id() { if [[ $1 == -u ]]; then printf 1000; else printf lab; fi; }
dpkg-query() {
    if [[ $* == *'${Version}'* ]]; then printf '5:29.8.2-test'; return 0; fi
    case ${!#} in
        docker-ce|docker-ce-cli|containerd.io) [[ $PREP_FRESH == 0 ]] && printf 'install ok installed' ;;
        "$PREP_CONFLICT") printf 'install ok installed' ;;
        *) return 1 ;;
    esac
}
sudo() {
    # sudo-rs reproducer: validation needs a terminal despite NOPASSWD,
    # whereas running the permitted command succeeds without one.
    [[ ${1:-} == -v ]] && { printf 'A terminal is required to authenticate\n' >&2; return 1; }
    if [[ ${1:-} == true ]]; then
        printf 'sudo true\n' >> "$PREP_LOG"
        [[ $PREP_SUDO_FAIL == 0 ]]; return
    fi
    "$@"
}
docker() {
    printf 'docker %s\n' "$*" >> "$PREP_LOG"
    if [[ $* == *'{{.Containers}}'* ]]; then printf '%s' "$PREP_CONTAINERS"; fi
}
apt-get() { printf 'apt %s\n' "$*" >> "$PREP_LOG"; [[ $PREP_APT_FAIL == 0 ]]; }
systemctl() { printf 'systemctl %s\n' "$*" >> "$PREP_LOG"; }
usermod() { printf 'usermod %s\n' "$*" >> "$PREP_LOG"; }
curl() { printf 'curl %s\n' "$*" >> "$PREP_LOG"; printf mock-key > "${!#}"; }
install() { printf 'install %s\n' "$*" >> "$PREP_LOG"; }
'''
            result = subprocess.run([BASH, '--noprofile', '--norc', '-s'], input=code + '\n' + extra + '\nmain ' + arguments + '\n',
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=20)
            return result, log.read_text() if log.exists() else ''

    def test_check_performs_no_system_operations(self):
        result, calls = self.run_mock('--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, '')

    def test_nopasswd_sudo_rs_without_terminal(self):
        result, calls = self.run_mock('--check', extra='prepare_sudo')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, 'sudo true\n')

    def test_denied_sudo_stops_before_system_mutations(self):
        result, calls = self.run_mock(sudo_fails=True, extra='prepare_sudo')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, 'sudo true\n')

    def test_clean_host_installs_and_verifies(self):
        result, calls = self.run_mock()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('apt install -y --no-install-recommends', calls)
        self.assertIn('docker-buildx-plugin docker-compose-plugin', calls)
        self.assertNotIn('install -y --no-install-recommends docker-ce ', calls)
        self.assertIn('docker-ce=5:29.8.2-test', calls)
        self.assertIn('usermod -aG docker lab', calls)
        self.assertIn('READY', result.stdout)

    def test_conflicting_package_stops_before_any_write(self):
        result, calls = self.run_mock(conflict='docker.io')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, '')

    def test_new_host_uses_official_repository(self):
        result, calls = self.run_mock(fresh=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('https://download.docker.com/linux/ubuntu/gpg', calls)
        self.assertIn('/etc/apt/sources.list.d/docker.sources', calls)
        self.assertIn('apt install -y --no-install-recommends docker-ce docker-ce-cli containerd.io', calls)

    def test_existing_stack_stops_before_apt(self):
        result, calls = self.run_mock(containers='9')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('apt ', calls)
        self.assertNotIn('usermod ', calls)

    def test_remote_daemon_is_refused(self):
        result, calls = self.run_mock(extra='export DOCKER_HOST=ssh://other')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, '')

    def test_install_failure_stops_before_daemon_and_group_changes(self):
        result, calls = self.run_mock(apt_fails=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('systemctl ', calls)
        self.assertNotIn('usermod ', calls)

    def test_other_platform_is_refused(self):
        result, calls = self.run_mock(extra='read_platform() { ID=debian; VERSION_ID=12; VERSION_CODENAME=bookworm; }')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, '')


if __name__ == '__main__':
    unittest.main()
