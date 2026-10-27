"""Detect package.json; follow this repository's existing Node release path."""
import json
from pathlib import Path
import subprocess
import sys


def run(*args):
    if sys.platform == "win32" and args[0] == "npm":
        args = ("npm.cmd", *args[1:])
    subprocess.run(args, check=True)


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    package = Path("package.json")
    if not package.exists():
        raise RuntimeError("项目已不再是 Node.js，拒绝将未构建源码冒充构建产物，请适配新构建方式。")
    data = json.loads(package.read_text(encoding="utf-8"))
    scripts = data.get("scripts", {})
    print("Detected Node.js package:", data["name"], data["version"])
    # 当前仓库仅有 peerDependency，无运行/开发依赖；不必联网安装。
    if data.get("dependencies") or data.get("devDependencies"):
        if not Path("package-lock.json").exists():
            raise RuntimeError("出现依赖却没有 npm lockfile，请人工锁定依赖。")
        run("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund")
    for file in ("lib/index.js", "lib/core.js", "lib/reapply.js"):
        run("node", "--check", file)
    run("node", "--input-type=module", "-e", "import './lib/index.js'")
    if "build" in scripts:
        run("npm", "run", "build")
    elif "build:client" in scripts:
        run("npm", "run", "build:client")
    if "test" in scripts:
        run("npm", "test")
    run("npm", "pack", "--ignore-scripts", "--pack-destination", str(out))
    if not list(out.glob("*.tgz")):
        raise RuntimeError("npm pack 未生成安装包。")
    # 与上游 Release 相同的 git archive，保留归属、LICENSE 和 NOTICE。
    run("git", "archive", "--format=zip", "--prefix=dsh-purge/",
        "--output=" + str(out / "release-source.zip"), "HEAD")


if __name__ == "__main__":
    main()
