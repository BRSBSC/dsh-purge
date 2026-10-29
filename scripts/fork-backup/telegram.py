"""Upload trusted artifact bytes, never execute their contents."""
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
import zipfile

MAX_FILE_SIZE = 45 * 1024 * 1024  # 为公共 Bot API 的单文件上限留出余量。


def send(name, content, message):
    token = os.environ["TG_BOT_TOKEN"]
    chat = os.environ["TG_CHAT_ID"]
    thread = os.environ.get("TG_MESSAGE_THREAD_ID", "").strip()
    if not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]+", chat):
        raise ValueError("TG_CHAT_ID 格式错误")
    if thread and (not thread.isdigit() or int(thread) <= 0):
        raise ValueError("TG_MESSAGE_THREAD_ID 必须为正整数或留空")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise ValueError("TG_BOT_TOKEN 格式错误")
    boundary = secrets.token_hex(20)
    fields = {"chat_id": chat, "rich_message": json.dumps(message, ensure_ascii=False)}
    if thread:
        fields["message_thread_id"] = thread
    body = bytearray()
    for key, value in fields.items():
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{safe_name}"\r\nContent-Type: application/zip\r\n\r\n'.encode())
    body.extend(content)
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    for attempt in range(4):
        request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendRichMessage",
                                         data=bytes(body), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.load(response)
            if result.get("ok"):
                print("源码 ZIP 富消息发送成功")
                return
            raise RuntimeError("Telegram API 返回 ok=false")
        except urllib.error.HTTPError as error:
            if error.code != 429:
                raise RuntimeError(f"Telegram HTTP {error.code}，检查 Bot 权限及 Chat/Thread ID") from None
            result = json.loads(error.read())
            delay = int(result.get("parameters", {}).get("retry_after", 5))
            if attempt == 3 or delay > 60:
                raise RuntimeError("Telegram 限流，请重跑") from None
            time.sleep(delay + 1)
        except (urllib.error.URLError, TimeoutError):
            # 不自动重发：超时可能已被 Telegram 接收；重跑可能产生重复消息。
            raise RuntimeError("Telegram 网络错误；检查后重跑，可能出现重复消息") from None


def main():
    path = Path(sys.argv[1]) / "source.zip"
    if not path.is_file():
        raise RuntimeError("没有待上传的 source.zip")
    if path.stat().st_size > MAX_FILE_SIZE:
        raise RuntimeError("源码 ZIP 超过单文件大小限制，停止发送；请从 GitHub Actions 下载。")
    sha = os.environ["BACKUP_SHA"]
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise ValueError("BACKUP_SHA 格式错误")
    with zipfile.ZipFile(path) as archive:
        def read(name):
            try:
                info = archive.getinfo("dsh-purge/" + name)
            except KeyError:
                return ""
            if info.file_size > 256 * 1024:
                return ""
            return archive.read(info).decode("utf-8-sig", errors="replace")

        try:
            package = json.loads(read("package.json"))
        except (ValueError, RecursionError):
            package = {}
        version = package.get("version") if isinstance(package, dict) else None
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.+-]{1,80}", version):
            version = None
        notes = ""
        if version:
            log = read("release-notes.md")
            section = re.search(r"^# " + re.escape(version) + r"\s*\n(.*?)(?=^# |\Z)", log, re.M | re.S)
            if section:
                chinese = re.search(r"^## 中文\s*\n(.*?)(?=^## |\Z)", section[1], re.M | re.S)
                notes = (chinese[1] if chinese else section[1]).strip()
    # shortcut: 当前更新日志只有粗体、行内代码与列表；格式变复杂时再引入 Markdown 解析。
    notes = notes.replace("**", "").replace("`", "")
    notes = re.sub(r"^- ", "• ", notes, flags=re.M)
    notes = notes or "该版本未提供更新说明。"
    name = f"dsh-purge-v{version}-{sha[:7]}.zip" if version else f"dsh-purge-{sha[:7]}.zip"
    message = {"blocks": [
        {"type": "heading", "text": "版本号", "size": 3},
        {"type": "paragraph", "text": f"dsh-purge v{version}" if version else f"未标注版本（提交 {sha[:7]}）"},
        {"type": "heading", "text": "更新说明", "size": 3},
        {"type": "paragraph", "text": notes},
        {"type": "heading", "text": "文件", "size": 3},
        {"type": "document", "document": {"type": "document", "media": "attach://document"},
         "caption": {"text": "完整源码，含 Git 提交历史；解压后按 GIT-HISTORY.md 恢复分支、标签及提交记录。"}},
    ]}
    send(name, path.read_bytes(), message)


if __name__ == "__main__":
    main()
