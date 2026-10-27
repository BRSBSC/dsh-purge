"""Upload trusted artifact bytes, never execute their contents."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
import urllib.error
import urllib.request

CHUNK = 45 * 1024 * 1024  # 低于公共 Bot API 的单文件 50 MB 限制。


def chunks(path):
    if path.stat().st_size <= CHUNK:
        yield path.name, path.read_bytes()
        return
    with path.open("rb") as source:
        part = 1
        while block := source.read(CHUNK):
            yield f"{path.name}.part{part:04d}", block
            part += 1


def send(name, content, caption):
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
    fields = {"chat_id": chat, "caption": caption[:1024]}
    if thread:
        fields["message_thread_id"] = thread
    body = bytearray()
    for key, value in fields.items():
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{safe_name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
    body.extend(content)
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    for attempt in range(4):
        request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument",
                                         data=bytes(body), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.load(response)
            if result.get("ok"):
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
    root = Path(sys.argv[1])
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if not files:
        raise RuntimeError("没有待上传文件")
    sums = []
    for path in files:
        for name, content in chunks(path):
            sums.append(f"{hashlib.sha256(content).hexdigest()}  {name}")
            send(name, content, f"{os.environ['GITHUB_REPOSITORY']}\n{os.environ['BACKUP_SHA']}\n{name}\n{os.environ['RUN_URL']}")
            time.sleep(1)
    send("SHA256SUMS.txt", ("\n".join(sums) + "\n").encode(), "SHA-256 校验清单；分片按编号拼接后恢复。")


if __name__ == "__main__":
    main()
