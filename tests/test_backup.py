"""Offline regression checks; no Telegram messages and no remote writes."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/fork-backup" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = load("sync")
tg = load("telegram")


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.temp.name)
        sync.git("init", "-b", "master")
        sync.git("config", "user.name", "Test")
        sync.git("config", "user.email", "test@example.invalid")
        self.commit("base.txt", "base")
        self.base = sync.ref("HEAD")

    def tearDown(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    def commit(self, name, content):
        file = Path(name)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        sync.git("add", name)
        sync.git("commit", "-m", "fixture")
        return sync.ref("HEAD")

    def test_normal_update_and_local_changes_survive(self):
        sync.git("checkout", "-b", "upstream")
        upstream = self.commit("new.txt", "upstream")
        sync.git("checkout", "master")
        self.commit("local.txt", "fork customization")
        sync.validate_update("HEAD", upstream, self.base)
        sync.git("merge", "--no-edit", upstream)
        self.assertEqual(Path("local.txt").read_text(), "fork customization")

    def test_rewritten_upstream_stops(self):
        old = self.commit("old.txt", "old")
        sync.git("reset", "--hard", self.base)
        new = self.commit("new.txt", "rewritten")
        with self.assertRaisesRegex(RuntimeError, "改写"):
            sync.validate_update("HEAD", new, old)

    def test_control_workflow_change_stops(self):
        upstream = self.commit(".github/workflows/evil.yml", "secrets")
        with self.assertRaisesRegex(RuntimeError, "控制文件"):
            sync.validate_update(self.base, upstream)

    def test_conflict_does_not_touch_remote(self):
        sync.git("checkout", "-b", "upstream")
        upstream = self.commit("base.txt", "upstream")
        sync.git("checkout", "master")
        head = self.commit("base.txt", "fork")
        result = sync.git("merge", "--no-edit", upstream, check=False)
        self.assertNotEqual(result.returncode, 0)
        sync.git("merge", "--abort")
        self.assertEqual(sync.ref("HEAD"), head)

    def test_bundle_restores_branches_tags_and_history(self):
        sync.git("branch", "saved-branch")
        sync.git("tag", "v1")
        self.commit("later.txt", "new")
        path = Path.cwd() / "history.bundle"
        sync.bundle(path)
        sync.git("clone", "--mirror", str(path), "restored.git")
        refs = sync.git("--git-dir=restored.git", "show-ref").stdout
        self.assertIn("refs/heads/saved-branch", refs)
        self.assertIn("refs/tags/v1", refs)
        self.assertEqual(sync.git("--git-dir=restored.git", "rev-list", "--count", "master").stdout.strip(), "2")

    def test_unavailable_upstream_still_leaves_prebackup(self):
        with patch.object(sync.urllib.request, "urlopen", side_effect=OSError("unavailable")), \
             patch("sys.argv", ["sync.py", "backup"]):
            with self.assertRaises(OSError):
                sync.main()
        self.assertTrue(Path("backup/before-sync.bundle").is_file())
        self.assertEqual(sync.ref("HEAD"), self.base)

    def test_split_roundtrip(self):
        path = Path("sample.bundle")
        path.write_bytes(b"123456789")
        with patch.object(tg, "CHUNK", 4):
            parts = list(tg.chunks(path))
        self.assertEqual([p[0] for p in parts], ["sample.bundle.part0001", "sample.bundle.part0002", "sample.bundle.part0003"])
        self.assertEqual(b"".join(p[1] for p in parts), path.read_bytes())

    def test_telegram_multipart_includes_topic_and_document(self):
        import io
        env = {"TG_BOT_TOKEN": "123:abc", "TG_CHAT_ID": "-100123", "TG_MESSAGE_THREAD_ID": "42"}
        with patch.dict(os.environ, env), \
             patch.object(tg.urllib.request, "urlopen", return_value=io.BytesIO(b'{"ok":true}')) as request:
            tg.send("history.bundle", b"backup", "commit abc")
        body = request.call_args.args[0].data
        self.assertIn(b'name="message_thread_id"\r\n\r\n42', body)
        self.assertIn(b'name="chat_id"\r\n\r\n-100123', body)
        self.assertIn(b'filename="history.bundle"', body)
        self.assertIn(b"backup", body)

    def test_atomic_rejection_keeps_remote_master_unchanged(self):
        remote = str(Path.cwd() / "remote.git")
        sync.git("init", "--bare", remote)
        sync.git("push", remote, "HEAD:refs/heads/master")
        old_upstream = self.commit("old.txt", "old upstream")
        sync.git("push", remote, "HEAD:refs/heads/backup-upstream")
        sync.git("reset", "--hard", self.base)
        new_upstream = self.commit("new.txt", "rewritten upstream")
        result = sync.git("push", "--atomic", remote, "HEAD:refs/heads/master",
                          new_upstream + ":refs/heads/backup-upstream", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sync.git("--git-dir=" + remote, "rev-parse", "master").stdout.strip(), self.base)
        self.assertEqual(sync.git("--git-dir=" + remote, "rev-parse", "backup-upstream").stdout.strip(), old_upstream)

    def test_full_sync_pushes_atomically_and_retries_until_delivered(self):
        import io
        real_git = sync.git
        remote = str(Path.cwd() / "origin.git")
        upstream_remote = str(Path.cwd() / "upstream.git")
        real_git("init", "--bare", remote)
        real_git("init", "--bare", upstream_remote)
        real_git("remote", "add", "origin", remote)
        real_git("push", "-u", "origin", "master")
        self.commit("new.txt", "new upstream")
        tip = sync.ref("HEAD")
        real_git("push", upstream_remote, "HEAD:refs/heads/master")
        real_git("reset", "--hard", self.base)

        def local_git(*args, **kwargs):
            return real_git(*(upstream_remote if value == "https://github.com/YuJunZhiXue/dsh-purge.git" else value
                              for value in args), **kwargs)

        output = str(Path.cwd() / "output.txt")
        for delivered in (False, True):
            if delivered:
                real_git("push", "origin", "HEAD:refs/heads/backup-delivered")
                real_git("fetch", "origin")
                real_git("remote", "remove", "upstream")
                Path(output).write_text("")
            with patch.object(sync, "git", side_effect=local_git), \
                 patch.object(sync.urllib.request, "urlopen", return_value=io.BytesIO(b'{"id":1339256252,"default_branch":"master"}')), \
                 patch.dict(os.environ, {"GITHUB_OUTPUT": output, "FORCE_BACKUP": "false"}), \
                 patch("sys.argv", ["sync.py", "backup"]):
                sync.main()
            self.assertEqual(real_git("--git-dir=" + remote, "rev-parse", "master").stdout.strip(), tip)
            self.assertEqual(real_git("--git-dir=" + remote, "rev-parse", "backup-upstream").stdout.strip(), tip)
            self.assertIn("changed=" + str(not delivered).lower(), Path(output).read_text())


if __name__ == "__main__":
    unittest.main()
