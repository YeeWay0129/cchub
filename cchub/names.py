"""名稱驗證、名稱解析、同名偵測、systemd-escape、路徑安全。"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass

from .paths import Config, UNIT_PREFIX
from .util import CchubError, is_within

NEW_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")   # 一律用 fullmatch（$ 會讓尾端 \n 過關）
RESERVED_NAMES = frozenset({"entry", "hub", "cchub"})
# stop/restart/logs 可以用這些字指入口
ENTRY_ALIASES = frozenset({"entry", "hub", "入口"})

# systemd 的單元名稱上限（UNIT_NAME_MAX = 256，含結尾 NUL）
UNIT_NAME_MAX = 255


def validate_new_name(name: str) -> str:
    """`new` 的名稱：格式 ^[a-z0-9][a-z0-9-]{0,39}$，保留字不能用。"""
    if not isinstance(name, str) or not NEW_NAME_RE.fullmatch(name):
        raise CchubError(
            f"新專案名稱「{name}」不合規則：只能用小寫英文、數字與 -，開頭不能是 -，最多 40 字"
            "（例如 ledger、my-tool-2）"
        )
    if name in RESERVED_NAMES:
        raise CchubError(f"「{name}」是保留字（{', '.join(sorted(RESERVED_NAMES))}），請換一個名稱")
    return name


# ---------------------------------------------------------------- systemd-escape

_VALID_ESCAPE_CHARS = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_.")


def _simplify_path(path: str) -> str:
    """模仿 systemd 的 path_simplify：去掉重複的 /、單獨的 .、結尾的 /。"""
    if not path.startswith("/"):
        raise CchubError(f"必須是絕對路徑：{path}")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise CchubError(f"路徑不能含 ..：{path}")
    return "/" + "/".join(parts)


def systemd_escape_path(path: str) -> str:
    """等同 `systemd-escape --path <path>`（systemd 255 的 unit_name_path_escape）。"""
    p = _simplify_path(path)
    if p == "/":
        return "-"
    raw = p.strip("/").encode("utf-8", "surrogateescape")
    out: list[str] = []
    for i, b in enumerate(raw):
        if b == 0x2F:  # '/'
            out.append("-")
        elif (i == 0 and b == 0x2E) or b not in _VALID_ESCAPE_CHARS:
            # 開頭的 '.' 也要跳脫；'-' 與 '\\' 不在合法字元內所以會被跳脫
            out.append("\\x%02x" % b)
        else:
            out.append(chr(b))
    return "".join(out)


_UNESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")


def systemd_unescape_path(instance: str) -> str:
    """`systemd-escape --unescape --path` 的反向（顯示用）。"""
    if instance == "-":
        return "/"
    buf = bytearray()
    i = 0
    while i < len(instance):
        m = _UNESCAPE_RE.match(instance, i)
        if m:
            buf.append(int(m.group(1), 16))
            i = m.end()
            continue
        ch = instance[i]
        buf.extend(b"/" if ch == "-" else ch.encode("utf-8"))
        i += 1
    return "/" + buf.decode("utf-8", "replace")


def unit_for_instance(instance: str) -> str:
    return f"{UNIT_PREFIX}{instance}.service"


def instance_for_dir(path: str) -> str:
    inst = systemd_escape_path(path)
    if len(unit_for_instance(inst)) > UNIT_NAME_MAX:
        raise CchubError(f"路徑太長，無法當作 systemd 實例名稱（跳脫後超過 {UNIT_NAME_MAX} 字元）：{path}")
    return inst


def instance_from_unit(unit: str) -> str | None:
    if unit.startswith(UNIT_PREFIX) and unit.endswith(".service"):
        return unit[len(UNIT_PREFIX):-len(".service")]
    return None


# ---------------------------------------------------------------- 路徑安全

def has_control_chars(s: str) -> bool:
    """控制字元（\\n、\\r、\\t…）與格式字元（零寬字元等）。"""
    return any(unicodedata.category(ch) in ("Cc", "Cf") for ch in s)


def has_worktree_component(path: str) -> bool:
    parts = [p for p in path.split("/") if p]
    return any(parts[i] == ".claude" and parts[i + 1] == "worktrees" for i in range(len(parts) - 1))


def check_path_safety(real: str, cfg: Config, home: str) -> str:
    """路徑安全：realpath 落在 allowed_roots 內；不能是根目錄本身、~，也不能在 .claude/worktrees/ 底下。"""
    home_r = os.path.realpath(home)
    roots = [os.path.realpath(r) for r in cfg.allowed_roots]
    if real in ("/", home_r):
        raise CchubError(f"不能對家目錄或根目錄操作：{real}")
    if real in roots:
        raise CchubError(f"不能是允許根目錄本身：{real}（請指定它底下的專案資料夾）")
    if not any(is_within(real, r) for r in roots):
        raise CchubError(f"路徑不在允許範圍內：{real}（允許：{', '.join(roots)}）")
    if has_worktree_component(real):
        raise CchubError(f"不能對 .claude/worktrees/ 底下的 worktree 操作：{real}")
    if not os.path.isdir(real):
        raise CchubError(f"不是資料夾：{real}")
    return real


@dataclass(frozen=True)
class Target:
    path: str          # realpath
    is_entry: bool
    given: str

    @property
    def name(self) -> str:
        return os.path.basename(self.path) or self.path


def resolve_target(target: str, cfg: Config, home: str) -> Target:
    """名稱解析：接受絕對路徑，或在 allowed_roots 底下找同名資料夾；同名兩個以上就拒絕並列出。"""
    given = target
    t = target or ""
    if not t.strip():
        raise CchubError("請給資料夾名稱或絕對路徑")
    if has_control_chars(t):
        raise CchubError(f"名稱或路徑含控制字元（例如換行、tab）：{t!r}")
    if t != t.strip():
        raise CchubError(f"名稱或路徑的前後不能有空白：{t!r}")
    entry_r = os.path.realpath(cfg.entry_dir)
    if t in ENTRY_ALIASES:
        return Target(entry_r, True, given)
    if t == "~" or t.startswith("~/"):
        t = os.path.join(home, t[2:]) if t != "~" else home
    if os.path.isabs(t):
        if has_worktree_component(os.path.normpath(t)):
            raise CchubError(f"不能對 .claude/worktrees/ 底下的 worktree 操作：{t}")
        if not os.path.isdir(t):
            raise CchubError(f"找不到資料夾：{t}")
        real = os.path.realpath(t)
    else:
        if "/" in t or t in (".", ".."):
            raise CchubError(f"只接受資料夾名稱或絕對路徑：{t}")
        candidates: list[str] = []
        for root in cfg.allowed_roots:
            p = os.path.join(root, t)
            if os.path.isdir(p):
                rp = os.path.realpath(p)
                if rp not in candidates:
                    candidates.append(rp)
        if not candidates:
            raise CchubError(f"找不到名為「{t}」的資料夾（搜尋範圍：{', '.join(cfg.allowed_roots)}）")
        if len(candidates) > 1:
            listing = "\n".join(f"  - {c}" for c in candidates)
            raise CchubError(f"有 {len(candidates)} 個名為「{t}」的資料夾，請改用完整路徑：\n{listing}")
        real = candidates[0]
    if real == entry_r:
        return Target(real, True, given)
    check_path_safety(real, cfg, home)
    return Target(real, False, given)
