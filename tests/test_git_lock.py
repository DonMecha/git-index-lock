"""CLIの引数区切り・終了コード・共有ロックを検証する。"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).resolve().parents[1] / "git_lock.py"
SPEC = importlib.util.spec_from_file_location("git_lock", TOOL)
git_lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(git_lock)


class GitLockCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = git_lock._mk_repo(Path(self.temp.name))

    def run_tool(self, *args):
        env = os.environ.copy()
        env.pop(git_lock.ENV_KEY, None)
        return subprocess.run(
            [sys.executable, str(TOOL), "-C", str(self.repo), *args],
            capture_output=True, text=True, env=env, timeout=10,
        )

    def test_bare_status_matches_explicit_git(self):
        (self.repo / "new.txt").write_text("new\n")
        bare = self.run_tool("--", "status", "--short")
        explicit = self.run_tool("--", "git", "status", "--short")
        self.assertEqual(bare.returncode, 0, bare.stderr)
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual(bare.stdout, explicit.stdout)
        self.assertIn("new.txt", bare.stdout)

    def test_git_pathspec_separator_is_preserved(self):
        target = self.repo / "--stat"
        target.write_text("before\n")
        initial = self.run_tool("--sh", "git add -- --stat && git commit -q -m 'fixture'")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        target.write_text("after\n")
        for prefix in (("git", "diff"), ("diff",)):
            with self.subTest(prefix=prefix):
                result = self.run_tool("--", *prefix, "--", "--stat")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("diff --git", result.stdout)
                self.assertIn("-before", result.stdout)
                self.assertIn("+after", result.stdout)

    def test_non_git_exit_code_is_preserved(self):
        result = self.run_tool("--", sys.executable, "-c", "raise SystemExit(17)")
        self.assertEqual(result.returncode, 17)

    def test_missing_executable_returns_127_and_releases_lock(self):
        result = self.run_tool("--", "git-index-lock-fixture-nonexistent")
        self.assertEqual(result.returncode, 127)
        self.assertIn("見つからない", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        following = self.run_tool("--nowait", "--", "status", "--short")
        self.assertEqual(following.returncode, 0, following.stderr)

    def test_permission_error_returns_126(self):
        executable = self.repo / "not-executable"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o600)
        result = self.run_tool("--", str(executable))
        self.assertEqual(result.returncode, 126)
        self.assertIn("実行権限がない", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_shorthand_waits_for_same_shared_lock(self):
        import fcntl
        with git_lock.lock_path(str(self.repo)).open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_tool("--nowait", "--", "status", "--short")
            self.assertEqual(result.returncode, git_lock.BUSY_EXIT)


if __name__ == "__main__":
    unittest.main()
