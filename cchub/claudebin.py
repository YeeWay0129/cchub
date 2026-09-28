"""挑版本最高的 claude 執行檔，並清掉會影響 CLI 的環境變數。

候選：
- ~/.config/Claude/claude-code/<版本>/claude   （桌面 App 帶的；App 更新會刪掉舊版目錄）
- ~/.local/share/claude/versions/<版本>        （原生更新器裝的；檔案本身就是執行檔）

挑最新版：伺服器是用自己的執行檔路徑開子 session，舊版目錄一被更新刪掉，就開不出新 session。
環境變數：CLAUDE*、ANTHROPIC* 等會被伺服器的 claude 繼承而改變它的行為（例如 ANTHROPIC_API_KEY
會讓它改用 API 金鑰，而 Remote Control 只接受 claude.ai 帳號登入），所以啟動前一律清掉。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping

VERSION_RE = re.compile(r"(\d{1,6})\.(\d{1,6})\.(\d{1,6})")   # 一律用 fullmatch：match 加 $ 會放過結尾的 \n
MIN_ARTIFACT_VERSION = (2, 1, 281)   # 2.1.281 起伺服器 session 才有 Artifact tool

ENV_DROP_PREFIXES = ("CLAUDE", "ANTHROPIC")
ENV_DROP_EXACT = frozenset({"USE_LOCAL_OAUTH", "USE_STAGING_OAUTH"})


def parse_version(s: str) -> tuple[int, int, int] | None:
    m = VERSION_RE.fullmatch(s or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def format_version(v: tuple[int, int, int]) -> str:
    return ".".join(str(x) for x in v)


@dataclass(frozen=True)
class ClaudeBinary:
    path: str
    version: tuple[int, int, int]
    source: str   # "native" 或 "desktop"

    @property
    def version_str(self) -> str:
        return format_version(self.version)


def _is_exec_file(p: str) -> bool:
    return os.path.isfile(p) and os.access(p, os.X_OK)


def find_candidates(native_root: str, desktop_root: str) -> list[ClaudeBinary]:
    out: list[ClaudeBinary] = []
    for root, source in ((native_root, "native"), (desktop_root, "desktop")):
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            v = parse_version(name)
            if v is None:
                continue
            p = os.path.join(root, name) if source == "native" else os.path.join(root, name, "claude")
            if _is_exec_file(p):
                out.append(ClaudeBinary(os.path.abspath(p), v, source))
    return out


def select_claude(native_root: str, desktop_root: str) -> tuple[ClaudeBinary | None, list[str]]:
    """回傳 (版本最高的執行檔, 警告)。同版本時優先用原生更新器的（桌面 App 更新會刪舊目錄）。"""
    cands = find_candidates(native_root, desktop_root)
    if not cands:
        return None, [f"找不到 claude 執行檔（找過 {native_root}/<版本> 與 {desktop_root}/<版本>/claude）"]
    best = max(cands, key=lambda c: (c.version, 1 if c.source == "native" else 0))
    warnings = []
    if best.version < MIN_ARTIFACT_VERSION:
        warnings.append(
            f"claude {best.version_str} 低於 {format_version(MIN_ARTIFACT_VERSION)}："
            "伺服器 session 沒有 Artifact tool"
        )
    return best, warnings


def clean_env(env: Mapping[str, str]) -> dict[str, str]:
    """清掉 CLAUDE*、ANTHROPIC*、USE_LOCAL_OAUTH、USE_STAGING_OAUTH；其餘（HOME、PATH…）保留。"""
    out = {}
    for k, v in env.items():
        ku = k.upper()
        if ku.startswith(ENV_DROP_PREFIXES) or ku in ENV_DROP_EXACT:
            continue
        out[k] = v
    return out
