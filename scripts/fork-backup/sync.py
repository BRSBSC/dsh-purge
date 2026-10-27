"""Trusted controller: backup first, merge safely, never force-push."""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import zipfile


def git(*args, check=True):
    result = subprocess.run(["git", *args], text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result


def ancestor(old, new):
    result = git("merge-base", "--is-ancestor", old, new, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr)
    return result.returncode == 0


def validate_update(head, upstream, previous=None):
    if previous and not ancestor(previous, upstream):
        raise RuntimeError("上游改写/回退历史，停止；请人工检查。")
    base = git("merge-base", head, upstream).stdout.strip()
    if not base:
        raise RuntimeError("无共同祖先，停止。")
    changed = git("diff", "--name-only", base, upstream, "--",
                  ".github/workflows", "scripts/fork-backup").stdout
    if changed:
        raise RuntimeError("上游修改自动化控制文件，请人工审查：\n" + changed)


def bundle(path):
    git("bundle", "create", str(path), "--all")
    git("bundle", "verify", str(path))


def ref(name):
    result = git("rev-parse", "--verify", name, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    # 即使上游已经消失，也先保存 fork 当前所有已获取的 refs。
    bundle(out / "before-sync.bundle")
    with urllib.request.urlopen(
        "https://api.github.com/repos/YuJunZhiXue/dsh-purge", timeout=30
    ) as response:
        metadata = json.load(response)
    if metadata["id"] != 1339256252 or metadata["default_branch"] != "master":
        raise RuntimeError("上游身份或默认分支改变，停止并人工检查。")
    git("remote", "add", "upstream", "https://github.com/YuJunZhiXue/dsh-purge.git")
    git("fetch", "--no-tags", "upstream",
        "+refs/heads/*:refs/remotes/upstream/*",
        "+refs/tags/*:refs/upstream-tags/*")
    head = ref("HEAD")
    upstream = ref("refs/remotes/upstream/master")
    if not upstream:
        raise RuntimeError("上游 master 不存在。")
    previous = ref("refs/remotes/origin/backup-upstream")
    validate_update(head, upstream, previous)
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    merged = git("merge", "--no-edit", upstream, check=False)
    if merged.returncode:
        git("merge", "--abort", check=False)
        raise RuntimeError("同步冲突，已停止；远程 master 未变。\n" + merged.stdout + merged.stderr)
    sha = ref("HEAD")
    bundle(out / "history.bundle")
    git("archive", "--format=zip", "--prefix=dsh-purge/",
        "--output=" + str(out / "source.zip"), sha)
    with zipfile.ZipFile(out / "source.zip", "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(out / "history.bundle", "dsh-purge/history.bundle")
        archive.writestr("dsh-purge/GIT-HISTORY.md", (
            "# Git 提交历史恢复\n\n"
            "history.bundle 保存本次已获取的 fork 和上游分支、标签及可达提交历史。\n"
            "不包含 LFS 实际文件、子模块仓库或 GitHub Issues/PR 等平台数据。\n\n"
            "安装 Git 后，在本文件所在目录执行（使用新的恢复目录）：\n\n```sh\n"
            "git clone --mirror history.bundle restored.git\n"
            "git --git-dir=restored.git fsck --full\n"
            "git --git-dir=restored.git symbolic-ref HEAD refs/heads/master\n"
            "git clone restored.git restored-worktree\n"
            f"git -C restored-worktree checkout {sha}\n"
            "git -C restored-worktree log --oneline\n```\n\n"
            "直接解压的源码可立即查看；恢复后的 restored-worktree 含 .git 与提交历史。\n"
        ))
    manifest = {"repository": os.environ.get("GITHUB_REPOSITORY"),
                "commit": sha, "upstream": upstream,
                "upstream_repository_id": metadata["id"],
                "history_scope": "all fetched fork/upstream branches and tags; no LFS objects/submodule repos"}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    # 单次原子、非强制推送；保护规则拒绝或并发更新都会安全失败。
    git("push", "--atomic", "origin", "HEAD:refs/heads/master",
        upstream + ":refs/heads/backup-upstream")
    delivered = ref("refs/remotes/origin/backup-delivered")
    changed = sha != delivered or os.environ.get("FORCE_BACKUP") == "true"
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"sha={sha}\nchanged={str(changed).lower()}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # 不打印网络请求 URL 或认证配置。
        print(f"::error::{type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
