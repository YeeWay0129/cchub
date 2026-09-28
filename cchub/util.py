"""共用小工具：錯誤型別、JSON 原子寫入、時間格式。"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any


class CchubError(Exception):
    """給使用者看的錯誤（訊息為繁體中文），CLI 以 exit 1 結束。"""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def read_json(path: str, default: Any = None) -> Any:
    """讀 JSON；檔案不存在回傳 default。解析失敗丟 CchubError。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        raise CchubError(f"無法讀取 {path}：{e}") from e


def fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: str, data: bytes, mode: int = 0o600) -> None:
    """寫暫存檔（mode）→ fsync → os.replace → fsync 目錄。"""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".cchub-", dir=d)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(d)


def atomic_write_text(path: str, text: str, mode: int = 0o600) -> None:
    # 先編碼完才碰檔案：編碼失敗（例如孤立 surrogate）時什麼都不寫
    atomic_write_bytes(path, text.encode("utf-8"), mode)


def dump_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def atomic_write_json(path: str, data: Any, mode: int = 0o600, trailing_newline: bool = True) -> None:
    text = dump_json(data)
    if trailing_newline:
        text += "\n"
    atomic_write_text(path, text, mode)


def ensure_dir(path: str, mode: int = 0o700) -> None:
    os.makedirs(path, mode=mode, exist_ok=True)


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}天{h}時"
    if h:
        return f"{h}時{m}分"
    if m:
        return f"{m}分"
    return f"{s}秒"


def iso_now(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() if ts is None else ts))


def is_within(path: str, root: str) -> bool:
    """path 是否等於 root 或在 root 底下（兩者都應是已正規化的絕對路徑）。"""
    root = root.rstrip("/") or "/"
    if root == "/":
        return path.startswith("/")
    return path == root or path.startswith(root + "/")
