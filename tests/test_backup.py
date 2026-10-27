"""Offline regression checks; no Telegram messages and no remote writes."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

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

    def source_zip(self, version="1.2.3", notes=None):
        path = Path("source.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("dsh-purge/package.json", json.dumps({"name": "dsh-purge", "version": version}))
            if notes is not None:
                archive.writestr("dsh-purge/release-notes.md", notes)
        return path

    def test_only_source_zip_is_sent_once_with_version_and_chinese_notes(self):
        path = self.source_zip(notes="# 1.2.3\n\n## 中文\n\n- 修复 **界面** 与 `配置`。\n- 支持 <新> & 旧版。\n\n## English\n\n- English notes\n\n# 1.2.2\n\n旧版说明")
        Path("history.bundle").write_bytes(b"history")
        Path("release-source.zip").write_bytes(b"duplicate")
        Path("package.tgz").write_bytes(b"package")
        Path("manifest.json").write_text("{}")
        with patch("sys.argv", ["telegram.py", "."]), \
             patch.dict(os.environ, {"BACKUP_SHA": "abcdef0123456789"}), \
             patch.object(tg, "send") as send:
            tg.main()
        send.assert_called_once()
        name, content, message = send.call_args.args
        self.assertEqual(name, "dsh-purge-v1.2.3-abcdef0.zip")
        self.assertEqual(content, path.read_bytes())
        blocks = message["blocks"]
        self.assertEqual([b["text"] for b in blocks if b["type"] == "heading"], ["版本号", "更新说明", "文件"])
        text = json.dumps(message, ensure_ascii=False)
        self.assertIn("1.2.3", text)
        self.assertIn("修复 界面 与 配置。", text)
        self.assertIn("支持 <新> & 旧版。", text)
        self.assertNotIn("English notes", text)
        self.assertNotIn("旧版说明", text)
        self.assertIn("含 Git 提交历史", text)
        self.assertNotIn("不含 Git 提交历史", text)
        self.assertEqual(blocks[-1]["document"], {"type": "document", "media": "attach://document"})

    def test_missing_or_different_version_notes_are_not_mislabelled(self):
        for notes in (None, "# 9.9.9\n\n## 中文\n\n其他版本内容"):
            with self.subTest(notes=notes):
                self.source_zip(notes=notes)
                with patch("sys.argv", ["telegram.py", "."]), \
                     patch.dict(os.environ, {"BACKUP_SHA": "abcdef0123456789"}), \
                     patch.object(tg, "send") as send:
                    tg.main()
                message = json.dumps(send.call_args.args[2], ensure_ascii=False)
                self.assertIn("未提供", message)
                self.assertNotIn("其他版本内容", message)

    def test_missing_or_oversized_source_is_not_sent(self):
        with patch("sys.argv", ["telegram.py", "."]), patch.object(tg, "send") as send:
            with self.assertRaisesRegex(RuntimeError, "source.zip"):
                tg.main()
            self.source_zip()
            with patch.object(tg, "MAX_FILE_SIZE", 1):
                with self.assertRaisesRegex(RuntimeError, "单文件"):
                    tg.main()
            send.assert_not_called()

    def test_invalid_source_version_is_not_sent(self):
        self.source_zip(version="bad\r\nversion")
        with patch("sys.argv", ["telegram.py", "."]), patch.object(tg, "send") as send:
            with self.assertRaisesRegex(ValueError, "版本号"):
                tg.main()
            send.assert_not_called()

    def test_telegram_multipart_includes_topic_and_document(self):
        env = {"TG_BOT_TOKEN": "123:abc", "TG_CHAT_ID": "-100123", "TG_MESSAGE_THREAD_ID": "42"}
        message = {"blocks": [{"type": "paragraph", "text": "版本与更新说明"},
                              {"type": "document", "document": {"type": "document", "media": "attach://document"}}]}
        with patch.dict(os.environ, env), \
             patch.object(tg.urllib.request, "urlopen", return_value=io.BytesIO(b'{"ok":true}')) as request:
            tg.send("source.zip", b"backup", message)
        self.assertTrue(request.call_args.args[0].full_url.endswith("/sendRichMessage"))
        body = request.call_args.args[0].data
        self.assertIn(b'name="message_thread_id"\r\n\r\n42', body)
        self.assertIn(b'name="chat_id"\r\n\r\n-100123', body)
        self.assertIn(b'filename="source.zip"', body)
        self.assertIn(b'name="rich_message"', body)
        self.assertIn(b'attach://document', body)
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
        real_git("branch", "saved-branch")
        real_git("tag", "v1")
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
            with zipfile.ZipFile("backup/source.zip") as archive:
                self.assertEqual(archive.read("dsh-purge/new.txt"), b"new upstream")
                self.assertIn(b"git clone --mirror", archive.read("dsh-purge/GIT-HISTORY.md"))
                history = archive.read("dsh-purge/history.bundle")
            bundled = Path("from-zip.bundle")
            bundled.write_bytes(history)
            restored = f"restored-{delivered}.git"
            real_git("clone", "--mirror", str(bundled.resolve()), restored)
            real_git("--git-dir=" + restored, "fsck", "--full")
            refs = real_git("--git-dir=" + restored, "show-ref").stdout
            self.assertIn("refs/heads/saved-branch", refs)
            self.assertIn("refs/tags/v1", refs)
            self.assertEqual(real_git("--git-dir=" + restored, "rev-parse", "master").stdout.strip(), tip)
            self.assertEqual(real_git("--git-dir=" + restored, "rev-list", "--count", "master").stdout.strip(), "2")
            self.assertEqual(real_git("--git-dir=" + remote, "rev-parse", "master").stdout.strip(), tip)
            self.assertEqual(real_git("--git-dir=" + remote, "rev-parse", "backup-upstream").stdout.strip(), tip)
            self.assertIn("changed=" + str(not delivered).lower(), Path(output).read_text())


if __name__ == "__main__":
    unittest.main()
