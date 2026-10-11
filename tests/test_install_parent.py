"""B06-1 install.py init-config: a parent directory that is swapped after preflight is found, not trusted.

The tool works by path below the parent it checked (mkdtemp, renameat2, fsync), so a parent replaced by a symlink or by
another directory would put the set somewhere nobody checked. The result must then be an honest TARGET_PARENT_CHANGED,
never CONFIG_PREPARED, and no secret may be shown. Reviewer A's finding 4 (2026-10-06).

Run: python tests/test_install_parent.py   (Linux; synthetic data and temporary directories only; skipped elsewhere)
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_fixtures as fx  # noqa: E402
from install_fixtures import install  # noqa: E402

linux = unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux (renameat2, modes)")


@linux
class ParentSwapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo, self.sha = fx.make_repo(self.tmp / "repo")
        self.parent = self.tmp / "cfg"
        self.parent.mkdir(mode=0o700)
        os.chmod(self.parent, 0o700)
        self.target = self.parent / "set"
        self.moved = self.tmp / "cfg-moved"
        self.other = self.tmp / "elsewhere"
        self.other.mkdir(mode=0o755)

    def swap_for_symlink(self):
        os.rename(self.parent, self.moved)
        os.symlink(self.other, self.parent)

    def swap_for_new_directory(self):
        os.rename(self.parent, self.moved)
        os.mkdir(self.parent, 0o700)

    def run_with(self, step, swap):
        def fault(at):
            if at == step:
                swap()
        return install.init_config(self.sha, str(self.target), str(self.repo), gen=fx.STUB_GEN, fault=fault)

    def assertNothingShown(self, report):
        self.assertIsNone(report.reveal)
        self.assertNotIn("CONFIG_PREPARED", report.lines)

    def test_a_parent_swapped_for_a_symlink_before_the_staging_writes_nothing(self):
        report = self.run_with("begin", self.swap_for_symlink)
        self.assertEqual(report.unverified, ["TARGET_PARENT_CHANGED"])
        self.assertEqual(report.exit_code(), 3)
        self.assertNothingShown(report)
        self.assertEqual(sorted(os.listdir(self.other)), [])
        self.assertEqual(sorted(os.listdir(self.moved)), [])

    def test_a_parent_replaced_by_another_directory_at_the_same_path_is_found_too(self):
        report = self.run_with("begin", self.swap_for_new_directory)
        self.assertEqual(report.unverified, ["TARGET_PARENT_CHANGED"])
        self.assertNothingShown(report)
        self.assertEqual(sorted(os.listdir(self.parent)), [])
        self.assertEqual(sorted(os.listdir(self.moved)), [])

    def test_a_swap_after_the_staging_exists_never_ends_as_a_prepared_set(self):
        report = self.run_with("before_publish", self.swap_for_symlink)
        self.assertTrue(report.failed)
        self.assertNothingShown(report)
        self.assertEqual(sorted(os.listdir(self.other)), [])             # nothing reached the directory it was swapped for

    def test_a_swap_right_after_the_publication_to_another_directory_is_unknown_and_says_where_the_set_is(self):
        report = self.run_with("renamed", self.swap_for_symlink)
        self.assertEqual(report.unverified, ["PUBLICATION_STATE_UNKNOWN"])
        self.assertNothingShown(report)
        self.assertEqual(sorted(os.listdir(self.moved / "set")), [".env", "install-record.json"])    # complete, untouched
        self.assertTrue(any("is now at" in line and str(self.moved / "set") in line for line in report.lines))
        self.assertEqual(sorted(os.listdir(self.other)), [])

    def test_a_parent_path_that_is_now_an_alias_of_the_same_directory_is_not_taken_for_the_checked_one(self):
        def alias():
            os.rename(self.parent, self.moved)
            os.symlink(self.moved, self.parent)                          # the target path still shows our set
        report = self.run_with("renamed", alias)
        self.assertEqual(report.unverified, ["TARGET_PARENT_CHANGED"])
        self.assertNothingShown(report)
        self.assertEqual(sorted(os.listdir(self.moved / "set")), [".env", "install-record.json"])    # complete, untouched

    def test_an_untouched_parent_is_published_as_before(self):
        report = self.run_with("never", lambda: None)
        self.assertFalse(report.failed, report.lines)
        self.assertIn("CONFIG_PREPARED", report.lines)
        self.assertEqual(sorted(os.listdir(self.target)), [".env", "install-record.json"])


if __name__ == "__main__":
    unittest.main()
