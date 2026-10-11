"""B06 bootstrap: .env is written atomically (N2) and created private (N3).

Run: python tests/test_bootstrap_env_write.py   (add -v for names)

Offline and synthetic: a temporary directory, no Docker, no network, never the repository's real .env.
Mode, umask and symlink tests need a POSIX filesystem and are skipped (and reported as skipped) elsewhere:
on Windows a chmod proves nothing about the ACL, so no mode claim is made there.

What it pins down
  * set_env keeps its behaviour (LF, order, comments, unknown keys, appended block) but replaces the file all
    at once: a failure at any point (open, write, fsync, chmod, replace, an interrupt) leaves the previous
    bytes, mtime and directory listing exactly as they were;
  * the file is never visible half-written, a symlinked .env stays a symlink, an existing file keeps its mode,
    a new one is 0600 whatever the umask, never wider at any moment;
  * a new .env is created complete or not at all and never replaces an existing file;
  * bootstrap.step_env reports a failed write as a failure, records no secret that was not stored, and warns
    about an .env readable by other users without silently changing it.
"""
import contextlib
import errno
import io
import os
import pathlib
import stat
import sys
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import test_bootstrap_key_safety as ks  # noqa: E402  (module, not its classes: nothing is collected twice)
import bootstrap  # noqa: E402
import envfile  # noqa: E402
from envfile import read_env  # noqa: E402

posix = unittest.skipUnless(os.name == "posix", "needs POSIX modes and links")

MIXED = b"A=1\r\n# keep me\r\nexport B=2\nC=3"
UPDATED = b"A=1\n# keep me\nB=22\nC=3\n\n# \xe2\x94\x80\xe2\x94\x80 Added by scripts/bootstrap.py \xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\nD=4\n"


class FailingHandle:
    """A binary file handle that accepts part of the data and then fails, like a full disk."""

    def __init__(self, real):
        self._real = real

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._real.close()
        return False

    def fileno(self):
        return self._real.fileno()

    def flush(self):
        self._real.flush()

    def write(self, data):
        self._real.write(data[:max(1, len(data) // 2)])
        self._real.flush()
        raise OSError(errno.ENOSPC, "disk full")


def failing_fdopen(fail_from=1, real=os.fdopen):
    """os.fdopen whose handles start failing at the `fail_from`-th call (1 = the first file written)."""
    calls = []

    def fdopen(fd, *args, **kwargs):
        calls.append(fd)
        handle = real(fd, *args, **kwargs)
        return FailingHandle(handle) if len(calls) >= fail_from else handle
    return fdopen


@contextlib.contextmanager
def umask(value):
    old = os.umask(value)
    try:
        yield
    finally:
        os.umask(old)


class EnvWriteBase(ks.Offline):
    def setUp(self):
        super().setUp()
        self.dir = self.tmp / "cfg"
        self.dir.mkdir()
        self.target = self.dir / ".env"

    def listing(self, directory=None):
        return sorted(os.listdir(directory or self.dir))

    def state(self, path=None):
        path = path or self.target
        info = os.stat(path)
        return path.read_bytes(), info.st_mtime_ns, stat.S_IMODE(info.st_mode)


class SetEnvTests(EnvWriteBase):
    def test_behaviour_is_unchanged_lf_order_comments_unknown_keys_and_the_appended_block(self):
        self.target.write_bytes(MIXED)
        envfile.set_env(self.target, {"B": "22", "D": "4"})
        self.assertEqual(self.target.read_bytes(), UPDATED)
        self.assertEqual(self.listing(), [".env"])

    def test_a_missing_file_is_created_with_the_block_only(self):
        envfile.set_env(self.target, {"X": "1"})
        self.assertEqual(read_env(self.target), {"X": "1"})
        self.assertEqual(self.listing(), [".env"])

    def test_no_updates_touches_nothing(self):
        self.target.write_bytes(MIXED)
        before = self.state()
        envfile.set_env(self.target, {})
        self.assertEqual(self.state(), before)
        self.assertEqual(self.listing(), [".env"])

    def test_a_failure_at_any_point_leaves_the_previous_file_exactly_as_it_was(self):
        failures = {
            "no temporary file can be made": mock.patch("tempfile.mkstemp", side_effect=PermissionError(errno.EACCES, "denied")),
            "the write fails half way": mock.patch("os.fdopen", failing_fdopen()),
            "fsync fails": mock.patch("os.fsync", side_effect=OSError(errno.EIO, "io")),
            "the rename fails": mock.patch("os.replace", side_effect=OSError(errno.ENOSPC, "full")),
            "the process is interrupted at the rename": mock.patch("os.replace", side_effect=KeyboardInterrupt),
        }
        if os.name == "posix":
            failures["chmod fails"] = mock.patch("os.fchmod", side_effect=PermissionError(errno.EPERM, "no"))
        for label, patch in failures.items():
            with self.subTest(label):
                self.target.write_bytes(MIXED)
                before, listing = self.state(), self.listing()
                with patch, self.assertRaises((OSError, KeyboardInterrupt)):
                    envfile.set_env(self.target, {"B": "22", "D": "4"})
                self.assertEqual(self.state(), before)                  # bytes, mtime and mode
                self.assertEqual(self.listing(), listing)               # and no temporary file behind
                envfile.set_env(self.target, {"B": "22", "D": "4"})     # nothing is stuck: the next write works
                self.assertEqual(self.target.read_bytes(), UPDATED)

    def test_the_target_is_complete_until_the_rename_and_the_new_content_is_complete_before_it(self):
        self.target.write_bytes(MIXED)
        seen = {}
        real = os.replace

        def spy(src, dst):
            seen["target"] = pathlib.Path(dst).read_bytes()
            seen["temp"] = pathlib.Path(src).read_bytes()
            seen["same_dir"] = os.path.dirname(os.path.realpath(src)) == os.path.dirname(os.path.realpath(dst))
            return real(src, dst)

        with mock.patch("os.replace", spy):
            envfile.set_env(self.target, {"B": "22", "D": "4"})
        self.assertEqual(seen["target"], MIXED)
        self.assertEqual(seen["temp"], UPDATED)
        self.assertTrue(seen["same_dir"], "the rename must stay on one filesystem to be atomic")
        self.assertEqual(self.target.read_bytes(), UPDATED)

    @posix
    def test_a_symlinked_env_stays_a_symlink_and_its_target_is_the_file_replaced(self):
        real_dir = self.tmp / "external"
        real_dir.mkdir()
        real_file = real_dir / ".env"
        real_file.write_bytes(MIXED)
        try:
            self.target.symlink_to(real_file)
        except OSError:
            self.skipTest("symlinks are not available")
        envfile.set_env(self.target, {"B": "22", "D": "4"})
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(os.readlink(self.target), str(real_file))
        self.assertEqual(real_file.read_bytes(), UPDATED)
        self.assertEqual(self.listing(), [".env"])
        self.assertEqual(self.listing(real_dir), [".env"])

    @posix
    def test_a_dangling_symlink_creates_the_file_it_points_to_private(self):
        real_dir = self.tmp / "external"
        real_dir.mkdir()
        real_file = real_dir / ".env"
        try:
            self.target.symlink_to(real_file)
        except OSError:
            self.skipTest("symlinks are not available")
        with umask(0):
            envfile.set_env(self.target, {"X": "1"})
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(read_env(real_file), {"X": "1"})
        self.assertEqual(stat.S_IMODE(real_file.stat().st_mode), 0o600)

    @posix
    def test_an_existing_mode_is_kept_and_never_wider_at_any_moment(self):
        for mode in (0o600, 0o640):
            with self.subTest(mode=oct(mode)):
                self.target.write_bytes(MIXED)
                os.chmod(self.target, mode)
                seen = []
                real = os.replace

                def spy(src, dst):
                    seen.append(stat.S_IMODE(os.stat(src).st_mode))
                    return real(src, dst)

                with umask(0), mock.patch("os.replace", spy):
                    envfile.set_env(self.target, {"B": "22", "D": "4"})
                self.assertEqual(seen, [mode])                         # the temporary file had it before the rename
                self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), mode)

    @posix
    def test_a_new_file_is_0600_whatever_the_umask(self):
        for mask in (0o000, 0o022, 0o077):
            with self.subTest(umask=oct(mask)):
                path = self.dir / f"new{mask}"
                with umask(mask):
                    envfile.set_env(path, {"X": "1"})
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class CreatePrivateTests(EnvWriteBase):
    def test_creates_the_file_complete(self):
        envfile.create_private(self.target, MIXED)
        self.assertEqual(self.target.read_bytes(), MIXED)
        self.assertEqual(self.listing(), [".env"])

    @posix
    def test_it_is_0600_from_the_first_byte_whatever_the_umask(self):
        for mask in (0o000, 0o022, 0o077):
            with self.subTest(umask=oct(mask)):
                path = self.dir / f"private{mask}"
                seen = []
                real = os.link

                def spy(src, dst, real=real):
                    seen.append(stat.S_IMODE(os.stat(src).st_mode))     # the content is complete and private BEFORE it appears
                    return real(src, dst)

                with umask(mask), mock.patch("os.link", spy):
                    envfile.create_private(path, MIXED)
                self.assertEqual(seen, [0o600])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_an_existing_file_is_never_replaced(self):
        self.target.write_bytes(MIXED)
        before, listing = self.state(), self.listing()
        with self.assertRaises(FileExistsError):
            envfile.create_private(self.target, b"NEW=1\n")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.listing(), listing)

    @posix
    def test_a_dangling_symlink_is_followed_and_stays_a_symlink(self):
        real_file = self.tmp / "external.env"
        try:
            self.target.symlink_to(real_file)
        except OSError:
            self.skipTest("symlinks are not available")
        envfile.create_private(self.target, MIXED)
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(real_file.read_bytes(), MIXED)
        self.assertEqual(stat.S_IMODE(real_file.stat().st_mode), 0o600)

    @posix
    def test_a_filesystem_that_refuses_links_gets_an_exclusive_private_create(self):
        for code in (errno.EPERM, errno.ENOTSUP, errno.EXDEV, errno.ENOSYS):
            with self.subTest(errno=errno.errorcode[code]):
                path = self.dir / f"nolink{code}"
                with umask(0), mock.patch("os.link", side_effect=OSError(code, "no links")):
                    envfile.create_private(path, MIXED)
                self.assertEqual(path.read_bytes(), MIXED)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual([n for n in self.listing() if n.endswith(".tmp")], [])

    def test_another_link_failure_creates_nothing(self):
        with mock.patch("os.link", side_effect=OSError(errno.EACCES, "denied")), self.assertRaises(OSError):
            envfile.create_private(self.target, MIXED)
        self.assertEqual(self.listing(), [])

    @posix
    def test_a_failed_write_leaves_no_partial_file_on_either_path(self):
        # the temporary file is the first handle written; after a refused link the exclusive create is the second
        cases = {"temp file": ((), 1),
                 "exclusive fallback": ((mock.patch("os.link", side_effect=OSError(errno.EPERM, "x")),), 2)}
        for label, (extra, fail_from) in cases.items():
            with self.subTest(label), contextlib.ExitStack() as stack:
                for patch in extra:
                    stack.enter_context(patch)
                stack.enter_context(mock.patch("os.fdopen", failing_fdopen(fail_from)))
                with self.assertRaises(OSError):
                    envfile.create_private(self.target, MIXED)
                self.assertEqual(self.listing(), [])


class StepEnvTests(EnvWriteBase):
    """bootstrap.step_env on a synthetic checkout: the example is the template, ENV_PATH the file."""

    def setUp(self):
        super().setUp()
        self.example_path.write_text(ks.SYNTHETIC_EXAMPLE, encoding="utf-8", newline="\n")

    def run_step(self):
        with self.capture() as out:
            bootstrap.step_env()
        return out

    def test_a_fresh_env_is_the_example_plus_four_generated_secrets_and_nothing_secret_is_printed(self):
        self.env_path.unlink()
        out = self.run_step()
        self.assertFalse(bootstrap._failed, out.text)
        values = read_env(self.env_path)
        self.assertEqual(bootstrap._created_secrets, ks.CREATED_NAMES)
        for name in ks.CREATED_NAMES:
            self.assertFalse(values[name].startswith("CHANGE_ME"), name)
            self.assertTrue(values[name] not in out.text, f"{name} printed")
        self.assertEqual(values["OPENAI_API_KEY"], ks.OPENAI_KEY)               # supplied: kept as it was
        self.assertEqual(self.listing(self.tmp).count(".env"), 1)

    @posix
    def test_a_fresh_env_is_private_from_creation(self):
        self.env_path.unlink()
        with umask(0):
            self.run_step()
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o600)

    def test_a_failed_write_is_a_failure_and_records_nothing_that_was_not_stored(self):
        self.write_env(POSTGRES_PASSWORD="CHANGE_ME_x", LANGFLOW_API_KEY=ks.OLD_KEY)
        before, listing = self.env_state(), self.listing(self.tmp)
        with mock.patch("os.replace", side_effect=OSError(errno.ENOSPC, f"disk full {ks.PG_PASSWORD}")):
            out = self.run_step()
        self.assertTrue(bootstrap._failed)
        self.assertEqual(bootstrap._created_secrets, [])
        self.assertEqual(self.env_state(), before)                             # bytes and mtime
        self.assertEqual(self.listing(self.tmp), listing)
        self.assertIn("could not be updated", out.stdout)
        self.assertNotIn("generated POSTGRES_PASSWORD", out.stdout)             # never claimed as generated
        self.assert_clean(out.text)

    def test_a_failed_creation_writes_nothing_and_fails(self):
        self.env_path.unlink()
        with mock.patch.object(bootstrap, "create_private", side_effect=PermissionError(errno.EACCES, "denied")):
            out = self.run_step()
        self.assertTrue(bootstrap._failed)
        self.assertFalse(self.env_path.exists())
        self.assertIn("could not be created", out.stdout)
        self.assertEqual(bootstrap._created_secrets, [])

    def test_an_env_that_appeared_meanwhile_is_filled_never_replaced(self):
        self.env_path.unlink()
        with mock.patch.object(bootstrap, "create_private", side_effect=FileExistsError):
            out = self.run_step()
        self.assertFalse(bootstrap._failed, out.text)
        self.assertIn("already exists", out.stdout)

    @posix
    def test_an_env_readable_by_others_is_warned_about_and_not_silently_changed(self):
        self.write_env()
        os.chmod(self.env_path, 0o644)
        out = self.run_step()
        self.assertIn("accessible to other users", out.stdout)
        self.assertIn("chmod 600", out.stdout)
        self.assertEqual(stat.S_IMODE(self.env_path.stat().st_mode), 0o644)

    @posix
    def test_a_private_env_gets_no_warning(self):
        self.write_env()
        os.chmod(self.env_path, 0o600)
        self.assertNotIn("accessible to other users", self.run_step().stdout)

    def test_the_missing_openai_key_message_no_longer_claims_a_health_check_reports_it(self):
        self.write_env(OPENAI_API_KEY="CHANGE_ME_openai")
        self.run_step()
        text = " ".join(bootstrap._summary)
        self.assertIn("OPENAI_API_KEY is not set", text)
        self.assertNotIn("reports it under missing_config", text)
        self.assertIn("No /health endpoint reports", text)


if __name__ == "__main__":
    unittest.main()
