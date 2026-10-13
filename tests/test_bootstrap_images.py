"""Offline: bootstrap rebuilds an image only when what its Dockerfile copies changed in git. No Docker; git is real."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import bootstrap  # noqa: E402

GIT = shutil.which("git")


def done(rc=0, out=""):
    return subprocess.CompletedProcess([], rc, out, "")


class CopySources(unittest.TestCase):
    def test_sources_flags_and_stage_copies(self):
        text = ("FROM node AS build\nCOPY frontend/ ./\nFROM python\nCOPY --chown=1000:1000 requirements.txt \\\n  ./\n"
                "COPY --from=build /dist ./static\nADD app ./app\n")
        self.assertEqual(bootstrap._copy_sources(text), ["frontend/", "requirements.txt", "app"])

    def test_what_cannot_be_pinned_falls_back_to_the_whole_context(self):
        for text in ("COPY *.txt ./\n", "COPY [\"a\", \"b\"]\n", "ADD https://example.org/x.tgz /x\n",
                     "RUN --mount=type=bind,source=.,target=/src true\nCOPY a b\n", "COPY onlyone\n"):
            with self.subTest(text=text):
                self.assertIsNone(bootstrap._copy_sources(text))

    def test_no_copy_means_only_the_dockerfile(self):
        self.assertEqual(bootstrap._copy_sources("FROM x\nRUN true\n"), [])


@unittest.skipUnless(GIT, "git is required")
class Fingerprint(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.git("init", "-q")
        (self.root / "svc" / "app").mkdir(parents=True)
        (self.root / "svc" / "Dockerfile").write_text("FROM python\nCOPY app/ ./app/\n", encoding="utf-8")
        (self.root / "svc" / "app" / "main.py").write_text("print(1)\n", encoding="utf-8")
        (self.root / "svc" / "notes.md").write_text("notes\n", encoding="utf-8")
        (self.root / "README.md").write_text("readme\n", encoding="utf-8")
        self.commit("first")
        patcher = mock.patch.object(bootstrap, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args):
        subprocess.run([GIT, "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@invalid", *args],
                       check=True, capture_output=True)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def fingerprint(self, context="svc"):
        return bootstrap._image_fingerprint({"build": {"context": str(self.root / context), "dockerfile": "Dockerfile"}})

    def test_stable_across_commits_that_do_not_touch_the_copied_sources(self):
        first = self.fingerprint()
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        (self.root / "svc" / "notes.md").write_text("changed\n", encoding="utf-8")
        self.commit("unrelated")
        self.assertEqual(self.fingerprint(), first)

    def test_changes_with_a_copied_source_or_the_dockerfile(self):
        first = self.fingerprint()
        (self.root / "svc" / "app" / "main.py").write_text("print(2)\n", encoding="utf-8")
        self.commit("code")
        second = self.fingerprint()
        self.assertNotEqual(second, first)
        (self.root / "svc" / "Dockerfile").write_text("FROM python:3.12\nCOPY app/ ./app/\n", encoding="utf-8")
        self.commit("base")
        self.assertNotIn(self.fingerprint(), (first, second))

    def test_an_uncommitted_change_is_not_pinned(self):
        (self.root / "svc" / "app" / "main.py").write_text("print(3)\n", encoding="utf-8")
        self.assertIsNone(self.fingerprint())

    def test_a_context_outside_the_checkout_is_not_pinned(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (pathlib.Path(outside.name) / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
        spec = {"build": {"context": outside.name, "dockerfile": "Dockerfile"}}
        if pathlib.Path(os.path.relpath(outside.name, self.root)).parts[:1] == ("..",):
            self.assertIsNone(bootstrap._image_fingerprint(spec))


class RefreshImages(unittest.TestCase):
    MODEL = {"services": {"svc": {"image": "edu_svc:1.0", "build": {"context": "/x", "dockerfile": "Dockerfile"}},
                          "nginx": {"image": "nginx:1.30.5-alpine"}}}

    def setUp(self):
        patcher = mock.patch.object(bootstrap, "_built_this_run", set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def refresh(self, *, current="sha256:old", marker=None, fingerprint="abc123abc123", git=True, build_rc=0,
                tags="1.0\nsrc-abc123abc123\nsrc-000000000000\n", rebuild_all=False, services=()):
        calls = []
        ids = {"edu_svc:1.0": current, f"edu_svc:src-{fingerprint}": marker}

        def fake_run(argv, **kw):
            calls.append(argv)
            if argv[:3] == ["docker", "compose", "config"]:
                return done(0, json.dumps(self.MODEL))
            if argv[:1] == ["git"]:
                return done(0 if git else 128, "true\n" if git else "")
            if argv[:3] == ["docker", "image", "inspect"]:
                value = ids.get(argv[-1])
                return done(0, value + "\n") if value else done(1)
            if argv[:3] == ["docker", "image", "ls"]:
                return done(0, tags)
            return done(0)

        def fake_compose(*args, **kw):
            calls.append(["compose", *args])
            if args[0] == "build":
                ids["edu_svc:1.0"] = "sha256:new"
            return done(build_rc)

        with mock.patch.object(bootstrap, "run", side_effect=fake_run), \
                mock.patch.object(bootstrap, "compose", side_effect=fake_compose), \
                mock.patch.object(bootstrap, "_image_fingerprint", return_value=fingerprint), \
                mock.patch.object(bootstrap, "report") as report, mock.patch.object(bootstrap, "fail") as fail:
            result = bootstrap._refresh_images(list(services), rebuild_all=rebuild_all)
        return result, calls, report, fail

    def test_a_matching_marker_skips_the_build(self):
        result, calls, report, fail = self.refresh(marker="sha256:old")
        self.assertTrue(result)
        self.assertNotIn(["compose", "build", "svc"], calls)
        self.assertEqual(report.call_args.args[0], "skip")

    def test_a_changed_source_keeps_the_previous_image_rebuilds_and_moves_the_marker(self):
        result, calls, report, fail = self.refresh(marker="sha256:older")
        self.assertTrue(result)
        self.assertIn(["docker", "tag", "sha256:old", "edu_svc:1.0-prev"], calls)
        self.assertIn(["compose", "build", "svc"], calls)
        self.assertIn(["docker", "tag", "sha256:new", "edu_svc:src-abc123abc123"], calls)
        self.assertIn(["docker", "image", "rm", "edu_svc:src-000000000000"], calls)
        self.assertNotIn(["docker", "image", "rm", "edu_svc:src-abc123abc123"], calls)
        self.assertIn("source changed", report.call_args.args[1])

    def test_an_image_without_any_source_tag_says_so(self):
        result, calls, report, fail = self.refresh(marker=None, tags="1.0\n")
        self.assertIn(["compose", "build", "svc"], calls)
        self.assertIn("no source tag yet", report.call_args.args[1])

    def test_a_missing_image_is_built_without_a_prev_tag(self):
        result, calls, report, fail = self.refresh(current=None)
        self.assertIn(["compose", "build", "svc"], calls)
        self.assertFalse(any(c[:2] == ["docker", "tag"] and c[-1].endswith("-prev") for c in calls))

    def test_an_unpinned_source_is_rebuilt_without_a_marker(self):
        result, calls, report, fail = self.refresh(fingerprint=None, marker="sha256:old")
        self.assertIn(["compose", "build", "svc"], calls)
        self.assertFalse(any(c[:2] == ["docker", "tag"] and "src-" in c[-1] for c in calls))
        self.assertIn("not committed", report.call_args.args[1])

    def test_rebuild_all_happens_once_per_run(self):
        self.refresh(marker="sha256:old", rebuild_all=True)
        result, calls, report, fail = self.refresh(marker="sha256:old", rebuild_all=True)
        self.assertNotIn(["compose", "build", "svc"], calls)

    def test_a_failed_build_stops_and_keeps_the_marker(self):
        result, calls, report, fail = self.refresh(marker="sha256:older", build_rc=1)
        self.assertFalse(result)
        fail.assert_called_once()
        self.assertFalse(any(c[:2] == ["docker", "tag"] and "src-" in c[-1] for c in calls))

    def test_outside_git_nothing_is_built_and_it_says_so(self):
        result, calls, report, fail = self.refresh(git=False)
        self.assertTrue(result)
        self.assertNotIn(["compose", "build", "svc"], calls)
        self.assertEqual(report.call_args.args[0], "warn")

    def test_services_not_named_are_left_alone(self):
        result, calls, report, fail = self.refresh(marker="sha256:older", services=["nginx"])
        self.assertTrue(result)
        self.assertNotIn(["compose", "build", "svc"], calls)


if __name__ == "__main__":
    unittest.main()
