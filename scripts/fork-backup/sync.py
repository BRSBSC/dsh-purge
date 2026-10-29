"""Trusted controller: archive observed history and mirror upstream without merging."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request
import zipfile


def git(*args, check=True, input=None):
    result = subprocess.run(["git", *args], text=True, capture_output=True, input=input)
    if check and result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result


def mark_delivered(sha, previous):
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or (previous and not re.fullmatch(r"[0-9a-f]{40}", previous)):
        raise ValueError("备份提交 SHA 格式错误")
    git("push", "--force-with-lease=refs/heads/backup-delivered:" + previous,
        "origin", sha + ":refs/heads/backup-delivered")


def bundle(path, *refs):
    git("bundle", "create", str(path), *(refs or ("--all",)))
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
    if metadata["id"] != 1339256252:
        raise RuntimeError("上游身份改变，停止并人工检查。")
    branch = metadata["default_branch"]
    git("check-ref-format", "refs/heads/" + branch)
    previous = ref("refs/remotes/origin/upstream-mirror") or ""
    delivered = ref("refs/remotes/origin/backup-delivered") or ""
    previous_manifest = {}
    if previous:
        previous_manifest = json.loads(git("show", previous + ":manifest.json").stdout)
        with (out / "source.zip").open("wb") as target:
            subprocess.run(["git", "show", previous + ":source.zip"], stdout=target, check=True)
        with zipfile.ZipFile(out / "source.zip") as archive:
            (out / "previous.bundle").write_bytes(archive.read("dsh-purge/history.bundle"))
        restored = git("bundle", "unbundle", str(out / "previous.bundle")).stdout
        for line in restored.splitlines():
            oid = line.split(" ", 1)[0]
            git("update-ref", "refs/archive/" + oid, oid)
    git("fetch", "--no-tags", "https://github.com/YuJunZhiXue/dsh-purge.git",
        "+refs/heads/*:refs/remotes/upstream/*",
        "+refs/tags/*:refs/upstream-tags/*")
    upstream = ref("refs/remotes/upstream/" + branch)
    if not upstream:
        raise RuntimeError("上游默认分支不存在。")
    sha = upstream
    source_refs = git("for-each-ref", "--format=%(refname)",
                      "refs/remotes/upstream/", "refs/upstream-tags/", "refs/archive/",
                      "refs/heads/master", "refs/remotes/origin/backup-upstream").stdout.splitlines()
    upstream_refs = dict(line.split(" ", 1) for line in git(
        "for-each-ref", "--format=%(refname) %(objectname)",
        "refs/remotes/upstream/", "refs/upstream-tags/").stdout.splitlines())
    manifest = {"repository": os.environ.get("GITHUB_REPOSITORY"),
                "commit": sha, "default_branch": branch, "upstream_refs": upstream_refs,
                "upstream_repository_id": metadata["id"],
                "history_scope": "fetched source refs and archived objects; no LFS/submodules"}
    snapshot = previous
    if not previous or any(previous_manifest.get(k) != manifest[k] for k in ("commit", "default_branch", "upstream_refs")):
        # 排除快照分支本身，避免把旧 ZIP 递归装入新的历史包。
        bundle(out / "history.bundle", *source_refs)
        git("archive", "--format=zip", "--prefix=dsh-purge/",
            "--output=" + str(out / "source.zip"), sha)
        with zipfile.ZipFile(out / "source.zip", "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(out / "history.bundle", "dsh-purge/history.bundle")
            archive.writestr("dsh-purge/GIT-HISTORY.md", (
                "# Git 提交历史恢复\n\n"
                "history.bundle 保存已获取的上游分支、标签及历史对象，也保留控制库 master 的历史。\n"
                "不包含 LFS 实际文件、子模块仓库或 GitHub Issues/PR 等平台数据。\n\n"
                "安装 Git 后，在本文件所在目录执行（使用新的恢复目录）：\n\n```sh\n"
                "git clone --mirror history.bundle restored.git\n"
                "git --git-dir=restored.git fsck --full\n"
                f"git --git-dir=restored.git update-ref refs/heads/restored-source {sha}\n"
                "git --git-dir=restored.git symbolic-ref HEAD refs/heads/restored-source\n"
                "git clone restored.git restored-worktree\n"
                "git -C restored-worktree log --oneline\n```\n\n"
                "直接解压的源码可立即查看；恢复后的 restored-worktree 含 .git 与提交历史。\n"
            ))
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        entries = []
        for name in ("source.zip", "manifest.json"):
            blob = git("hash-object", "-w", str(out / name)).stdout.strip()
            entries.append(f"100644 blob {blob}\t{name}\0")
        tree = git("mktree", "-z", input="".join(entries)).stdout.strip()
        parents = ("-p", previous) if previous else ()
        snapshot = git("-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                       "commit-tree", tree, *parents, "-m", "Upstream snapshot " + sha).stdout.strip()
    else:
        (out / "manifest.json").write_text(json.dumps(previous_manifest, indent=2) + "\n", encoding="utf-8")
    # 只推控制器生成的数据快照，不把上游工作流或原始提交推入 GitHub。
    git("push", "--atomic", "--force-with-lease=refs/heads/upstream-mirror:" + previous,
        "origin", snapshot + ":refs/heads/upstream-mirror")
    receipt = git("show", delivered + ":manifest.json", check=False) if delivered else None
    delivered_sha = json.loads(receipt.stdout).get("commit") if receipt and receipt.returncode == 0 else None
    changed = sha != delivered_sha or os.environ.get("FORCE_BACKUP") == "true"
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"sha={sha}\nsnapshot={snapshot}\nchanged={str(changed).lower()}\nprevious_delivered={delivered}\n")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "--mark-delivered":
            mark_delivered(os.environ["BACKUP_SHA"], os.environ["PREVIOUS_DELIVERED"])
        else:
            main()
    except Exception as error:
        # 不打印网络请求 URL 或认证配置。
        print(f"::error::{type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
