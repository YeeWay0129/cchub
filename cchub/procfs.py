"""讀 /proc：自己在不在 cchub 單元裡、掃同 cwd 的 rc 程序、執行檔是否已被刪（CLI 更新會刪掉舊版執行檔）。

根目錄可注入（測試用假的 /proc 樹）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .claudebin import VERSION_RE

UNIT_IN_CGROUP_RE = re.compile(r"(cchub-rc@[^/\s]+\.service|cchub-reconcile\.service)")


@dataclass(frozen=True)
class RcProcess:
    pid: int
    cwd: str
    argv: tuple[str, ...]
    cgroup: str

    @property
    def cchub_unit(self) -> str | None:
        m = UNIT_IN_CGROUP_RE.search(self.cgroup)
        return m.group(1) if m else None


def _looks_like_claude(argv0: str, exe: str) -> bool:
    base = os.path.basename(argv0)
    if base == "claude" or VERSION_RE.fullmatch(base):
        return True
    exe = exe.replace(" (deleted)", "")
    return os.path.basename(exe) == "claude" or "/claude/versions/" in exe or "/claude-code/" in exe


def is_rc_argv(argv: tuple[str, ...], exe: str = "") -> bool:
    """argv 是不是 `claude remote-control …`／`claude rc …`。"""
    if len(argv) < 2 or not _looks_like_claude(argv[0], exe):
        return False
    if argv[1] in ("remote-control", "rc"):
        return True
    return "remote-control" in argv[1:4]


class ProcFS:
    def __init__(self, root: str = "/proc"):
        self.root = root

    def _p(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def read_cgroup(self, pid: str | int = "self") -> str:
        try:
            with open(self._p(str(pid), "cgroup"), "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def current_unit(self) -> str | None:
        """自己所在的 cchub 單元名稱；不在單元裡回傳 None（用來判斷是不是要對自己所在的單元動手）。"""
        m = UNIT_IN_CGROUP_RE.search(self.read_cgroup("self"))
        return m.group(1) if m else None

    def in_cchub_unit(self) -> bool:
        cg = self.read_cgroup("self")
        return "cchub-rc@" in cg or "cchub-reconcile" in cg

    def argv(self, pid: int) -> tuple[str, ...]:
        try:
            with open(self._p(str(pid), "cmdline"), "rb") as f:
                raw = f.read()
        except OSError:
            return ()
        parts = raw.split(b"\0")
        if parts and parts[-1] == b"":
            parts = parts[:-1]
        return tuple(p.decode("utf-8", "surrogateescape") for p in parts)

    def readlink(self, pid: int, what: str) -> str | None:
        try:
            return os.readlink(self._p(str(pid), what))
        except OSError:
            return None

    def pids(self) -> list[int]:
        try:
            return sorted(int(n) for n in os.listdir(self.root) if n.isdigit())
        except OSError:
            return []

    def rc_processes(self) -> list[RcProcess]:
        out = []
        for pid in self.pids():
            argv = self.argv(pid)
            if not argv:
                continue
            exe = self.readlink(pid, "exe") or ""
            if not is_rc_argv(argv, exe):
                continue
            cwd = self.readlink(pid, "cwd")
            if cwd is None:
                continue
            out.append(RcProcess(pid, cwd.replace(" (deleted)", ""), argv, self.read_cgroup(pid)))
        return out

    def foreign_rc_for(self, directory: str) -> list[RcProcess]:
        """cwd 等於 directory、但不屬於任何 cchub 單元的 rc 程序（例如終端機手動開的）。"""
        d = os.path.realpath(directory)
        return [p for p in self.rc_processes() if p.cwd == d and p.cchub_unit is None]

    def rc_under(self, directory: str) -> list[RcProcess]:
        d = os.path.realpath(directory).rstrip("/")
        return [p for p in self.rc_processes() if p.cwd == d or p.cwd.startswith(d + "/")]

    def exe_deleted(self, pid: int) -> bool | None:
        """/proc/<pid>/exe 指向已刪除的檔案 → True；程序不存在或讀不到 → None。"""
        link = self.readlink(pid, "exe")
        if link is None:
            return None
        return link.endswith(" (deleted)")

    def pid_in_unit(self, pid: int, unit: str) -> bool:
        return unit in self.read_cgroup(pid)
