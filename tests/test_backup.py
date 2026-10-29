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

    def setup_remotes(self):
        self.origin = str(Path.cwd() / "origin.git")
        self.upstream = str(Path.cwd() / "upstream.git")
        sync.git("init", "--bare", self.origin)
        sync.git("init", "--bare", self.upstream)
        sync.git("remote", "add", "origin", self.origin)
        sync.git("push", "-u", "origin", "master")
        sync.git("checkout", "-b", "source")

    def publish(self, branch="master"):
        tip = sync.ref("HEAD")
        sync.git("push", "--force", self.upstream, f"HEAD:refs/heads/{branch}")
        sync.git("checkout", "master")
        return tip

    def run_sync(self, branch="master", force=False, before_push=None, repository_id=1339256252):
        real_git = sync.git
        real_git("fetch", "origin")

        def local_git(*args, **kwargs):
            if args[0] == "push" and before_push:
                before_push()
            return real_git(*(self.upstream if v == "https://github.com/YuJunZhiXue/dsh-purge.git" else v
                              for v in args), **kwargs)

        output = Path.cwd() / "output.txt"
        output.write_text("")
        metadata = json.dumps({"id": repository_id, "default_branch": branch}).encode()
        with patch.object(sync, "git", side_effect=local_git), \
             patch.object(sync.urllib.request, "urlopen", return_value=io.BytesIO(metadata)), \
             patch.dict(os.environ, {"GITHUB_OUTPUT": str(output), "FORCE_BACKUP": str(force).lower()}), \
             patch("sys.argv", ["sync.py", "backup"]):
            sync.main()
        return dict(line.split("=", 1) for line in output.read_text().splitlines())

    def remote_sha(self, ref):
        return sync.git("--git-dir=" + self.origin, "rev-parse", ref).stdout.strip()

    def remote_source(self):
        return json.loads(sync.git("--git-dir=" + self.origin, "show", "upstream-mirror:manifest.json").stdout)["commit"]

    def run_fresh_sync(self, **kwargs):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as runner:
            sync.git("clone", self.origin, runner)
            os.chdir(runner)
            try:
                result = self.run_sync(**kwargs)
                (previous / "backup").mkdir(exist_ok=True)
                (previous / "backup/source.zip").write_bytes(Path("backup/source.zip").read_bytes())
                return result
            finally:
                os.chdir(previous)

    def test_mirror_accepts_conflicts_and_upstream_controller_changes_without_touching_master(self):
        self.commit(".github/workflows/fork-backup.yml", "trusted controller")
        controller = self.commit("local.txt", "fork customization")
        self.setup_remotes()
        self.commit("base.txt", "upstream conflict")
        self.commit(".github/workflows/fork-backup.yml", "untrusted workflow")
        tip = self.commit("scripts/fork-backup/sync.py", "raise RuntimeError('never execute')")
        self.publish()
        result = self.run_sync()
        self.assertEqual(self.remote_sha("master"), controller)
        self.assertEqual(sync.ref("HEAD"), controller)
        self.assertEqual(Path(".github/workflows/fork-backup.yml").read_text(), "trusted controller")
        self.assertEqual(self.remote_source(), tip)
        files = sync.git("--git-dir=" + self.origin, "ls-tree", "--name-only", "upstream-mirror").stdout.splitlines()
        self.assertEqual(files, ["manifest.json", "source.zip"])
        self.assertEqual(result["sha"], tip)
        self.assertEqual(result["changed"], "true")
        with zipfile.ZipFile("backup/source.zip") as archive:
            self.assertEqual(archive.read("dsh-purge/base.txt"), b"upstream conflict")
            self.assertEqual(archive.read("dsh-purge/.github/workflows/fork-backup.yml"), b"untrusted workflow")

    def test_rewrite_rollback_and_unrelated_renamed_branch_preserve_previous_history(self):
        self.setup_remotes()
        old = self.commit("old.txt", "old history")
        sync.git("tag", "-a", "upstream-v1", "-m", "original tag")
        old_tag = sync.ref("refs/tags/upstream-v1")
        sync.git("push", self.upstream, "refs/tags/upstream-v1")
        self.publish()
        self.run_sync()
        sync.git("push", "origin", old + ":refs/heads/backup-delivered")
        sync.git("checkout", "source")
        sync.git("reset", "--hard", self.base)
        rewritten = self.commit("new.txt", "rewritten history")
        sync.git("tag", "-f", "-a", "upstream-v1", "-m", "rewritten tag")
        new_tag = sync.ref("refs/tags/upstream-v1")
        sync.git("push", "--force", self.upstream, "refs/tags/upstream-v1")
        self.publish()
        self.assertEqual(self.run_fresh_sync()["changed"], "true")
        self.assertEqual(self.remote_source(), rewritten)
        sync.git("checkout", "source")
        sync.git("reset", "--hard", self.base)
        self.publish()
        self.assertEqual(self.run_fresh_sync()["changed"], "true")
        self.assertEqual(self.remote_source(), self.base)
        sync.git("checkout", "--orphan", "renamed")
        sync.git("rm", "-rf", ".")
        unrelated = self.commit("fresh.txt", "unrelated repository history")
        self.publish("new-main")
        result = self.run_fresh_sync(branch="new-main")
        self.assertEqual(result["sha"], unrelated)
        self.assertEqual(self.remote_sha("master"), self.base)
        with zipfile.ZipFile("backup/source.zip") as archive:
            Path("from-zip.bundle").write_bytes(archive.read("dsh-purge/history.bundle"))
            self.assertIn(b"git clone --mirror", archive.read("dsh-purge/GIT-HISTORY.md"))
        sync.git("clone", "--mirror", str(Path("from-zip.bundle").resolve()), "restored.git")
        sync.git("--git-dir=restored.git", "fsck", "--full")
        for sha in (old, rewritten, self.base, unrelated):
            sync.git("--git-dir=restored.git", "cat-file", "-e", sha + "^{commit}")
        for tag in (old_tag, new_tag):
            sync.git("--git-dir=restored.git", "cat-file", "-e", tag + "^{tag}")
        self.assertEqual(sync.git("--git-dir=" + self.origin, "rev-list", "--count", "upstream-mirror").stdout.strip(), "4")
        self.assertEqual(self.remote_source(), unrelated)
        self.assertFalse(any("source.zip" in line for line in sync.git("--git-dir=restored.git", "rev-list", "--objects", "--all").stdout.splitlines()))
        sync.git("--git-dir=restored.git", "update-ref", "refs/heads/restored-source", unrelated)
        sync.git("--git-dir=restored.git", "symbolic-ref", "HEAD", "refs/heads/restored-source")
        sync.git("clone", "restored.git", "restored-worktree")
        self.assertEqual(sync.git("-C", "restored-worktree", "rev-parse", "HEAD").stdout.strip(), unrelated)
        self.assertEqual(Path("restored-worktree/fresh.txt").read_text(), "unrelated repository history")
        self.assertEqual(result["previous_delivered"], old)

    def test_mirror_concurrent_update_rejects_entire_push(self):
        self.setup_remotes()
        first = self.commit("one.txt", "one")
        self.publish()
        self.run_sync()
        sync.git("checkout", "source")
        second = self.commit("two.txt", "two")
        self.publish()
        real_git = sync.git
        def race():
            real_git("push", "--force", self.origin, self.base + ":refs/heads/upstream-mirror")
        with self.assertRaises(RuntimeError):
            self.run_sync(before_push=race)
        self.assertEqual(self.remote_sha("upstream-mirror"), self.base)
        self.assertEqual(self.remote_sha("master"), self.base)
        self.assertNotEqual(first, second)
        self.assertEqual(len(sync.git("--git-dir=" + self.origin, "for-each-ref", "--format=%(refname)").stdout.splitlines()), 2)

    def test_changed_repository_identity_is_rejected(self):
        self.setup_remotes()
        self.publish()
        with self.assertRaisesRegex(RuntimeError, "身份"):
            self.run_sync(repository_id=1)
        self.assertEqual(self.remote_sha("master"), self.base)

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

    def test_missing_or_invalid_version_still_sends_source_with_commit_identifier(self):
        for version in (None, "bad\r\nversion"):
            self.source_zip(version=version)
            with patch("sys.argv", ["telegram.py", "."]), \
                 patch.dict(os.environ, {"BACKUP_SHA": "abcdef0123456789"}), \
                 patch.object(tg, "send") as send:
                tg.main()
            self.assertEqual(send.call_args.args[0], "dsh-purge-abcdef0.zip")
            self.assertIn("未标注版本", json.dumps(send.call_args.args[2], ensure_ascii=False))
        for package in (None, "{bad json", "[]", '{"version":' + '9' * 5000 + '}', '[' * 1500 + '0' + ']' * 1500):
            with zipfile.ZipFile("source.zip", "w") as archive:
                archive.writestr("dsh-purge/README.md", "source")
                if package is not None:
                    archive.writestr("dsh-purge/package.json", package)
            with patch("sys.argv", ["telegram.py", "."]), \
                 patch.dict(os.environ, {"BACKUP_SHA": "abcdef0123456789"}), \
                 patch.object(tg, "send") as send:
                tg.main()
            send.assert_called_once()

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

    def test_retries_until_delivered_then_skips_and_force_resends(self):
        self.setup_remotes()
        tip = self.commit("new.txt", "new upstream")
        self.publish()
        result = self.run_sync()
        self.assertEqual(result["changed"], "true")
        self.assertEqual(result["previous_delivered"], "")
        self.assertEqual(self.run_sync()["changed"], "true")
        sync.mark_delivered(result["snapshot"], "")
        self.assertEqual(self.run_fresh_sync()["changed"], "false")
        self.assertEqual(self.remote_sha("upstream-mirror"), result["snapshot"])
        self.assertEqual(self.run_sync(force=True)["changed"], "true")

    def test_delivery_marker_accepts_rollback_but_rejects_stale_receipt(self):
        self.setup_remotes()
        tip = self.commit("new.txt", "new upstream")
        self.publish()
        first = self.run_sync()
        sync.mark_delivered(first["snapshot"], "")
        sync.git("checkout", "source")
        sync.git("reset", "--hard", self.base)
        self.publish()
        rolled_back = self.run_sync()
        self.assertEqual(rolled_back["changed"], "true")
        sync.mark_delivered(rolled_back["snapshot"], first["snapshot"])
        self.assertEqual(self.remote_sha("backup-delivered"), rolled_back["snapshot"])
        with self.assertRaises(RuntimeError):
            sync.mark_delivered(first["snapshot"], first["snapshot"])
        self.assertEqual(self.remote_sha("backup-delivered"), rolled_back["snapshot"])
        self.assertEqual(self.remote_sha("master"), self.base)


if __name__ == "__main__":
    unittest.main()
