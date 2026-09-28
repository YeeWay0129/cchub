"""信任相關：F6 判定演算法、§5.1 規則 5 的 open 政策、§5.2 的窄範圍信任寫入。

設計重點（不可違反）：
- 唯一會寫信任的入口是 grant_trust_for_new_project()，只給 `cchub new` 在同一次呼叫、
  剛建立、內容只有模板的資料夾使用。**沒有**任何「信任既有資料夾」的函式。
- 寫 ~/.claude.json 時用和 CLI 相同的 mkdir 鎖（<config>.lock），鎖內讀→備份→只改目標鍵→原子寫。

F6 演算法依 CLI 判定信任的行為：
1. 先看「專案鍵」：資料夾所在 git 的 canonical 根（worktree → 主 repo 根），不在 git 裡就是資料夾本身。
2. 再從資料夾往上走，每層看 projects[<層>].hasTrustDialogAccepted，**遇到 git 根就停**。
保守差異：家目錄不算（F6：家目錄不存信任），也不會走到家目錄以上。
"""

from __future__ import annotations

import copy
import json
import os
import stat
import time
from dataclasses import dataclass
from typing import Callable

from .paths import Paths
from .util import CchubError, atomic_write_bytes, dump_json, ensure_dir, is_within, read_json

# §5.2 範本資料夾只能有這些東西：名稱 → 型別
TEMPLATE_ENTRIES = {"CLAUDE.md": "file", ".gitignore": "file", ".git": "dir"}
PENDING_MAX_AGE = 60.0          # 暫存紀錄的有效秒數
LOCK_RETRY_INTERVAL = 0.1       # mkdir 鎖重試間隔
LOCK_TIMEOUT = 15.0             # 最多等 15 秒
LOCK_STALE_AGE = 60.0           # 鎖的 mtime 超過 60 秒視為殘留
BACKUP_PREFIX = "cchub-claude.json."
BACKUP_KEEP = 10
READBACK_DELAY = 2.0
READBACK_ATTEMPTS = 3

# CLI 自己建立新 project 項目時帶的預設欄位（CLI 讀專案設定時不會合併預設值，所以新項目要帶齊這些欄位）
CLI_PROJECT_DEFAULTS = {
    "allowedTools": [],
    "mcpContextUris": [],
    "mcpServers": {},
    "enabledMcpjsonServers": [],
    "disabledMcpjsonServers": [],
    "hasTrustDialogAccepted": False,
    "hasClaudeMdExternalIncludesApproved": False,
    "hasClaudeMdExternalIncludesWarningShown": False,
}

# §5.1 規則 5：規格列的是 hooks、permissions 與 .mcp.json。
# 保守擴充：其他同樣會在 session 啟動時執行指令或載入 MCP 的設定鍵也算。
SPEC_RISKY_KEYS = ("hooks", "permissions")
EXTRA_RISKY_KEYS = (
    "env", "apiKeyHelper", "statusLine", "mcpServers", "enabledMcpjsonServers",
    "enableAllProjectMcpServers", "awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper",
)
RISKY_SETTINGS_KEYS = SPEC_RISKY_KEYS + EXTRA_RISKY_KEYS


class TrustRefused(CchubError):
    """§5.2 自我驗證不通過：不寫信任。"""


# ---------------------------------------------------------------- 讀 ~/.claude.json

def load_claude_config(path: str) -> dict:
    data = read_json(path, default={})
    if not isinstance(data, dict):
        raise CchubError(f"{path} 格式不是 JSON 物件")
    return data


def projects_of(claude_cfg: dict) -> dict:
    p = claude_cfg.get("projects")
    return p if isinstance(p, dict) else {}


# ---------------------------------------------------------------- git 根

def _is_git_marker(git_path: str, base: str) -> bool:
    """<dir>/.git 是資料夾或檔案才算（符號連結要解得開且不指回自己）。"""
    try:
        st = os.lstat(git_path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.path.realpath(git_path)
            if target == os.path.realpath(base):
                return False
            st = os.stat(git_path)
        except OSError:
            return False
    return stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)


def find_git_root(path: str) -> str | None:
    """從 path 往上找第一個含 .git（資料夾或檔案）的資料夾。"""
    cur = os.path.abspath(path)
    while True:
        if _is_git_marker(os.path.join(cur, ".git"), cur):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _read_small(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read(4096).strip()


def canonical_repo_root(git_root: str) -> str:
    """worktree（.git 是檔案）→ 主 repo 的根；其他情況回傳 git_root 本身。任何驗證失敗都回傳 git_root。"""
    dotgit = os.path.join(git_root, ".git")
    try:
        if os.path.isdir(dotgit):
            return git_root
        content = _read_small(dotgit)
        if not content.startswith("gitdir:"):
            return git_root
        gitdir = os.path.normpath(os.path.join(git_root, content[len("gitdir:"):].strip()))
        common_rel = _read_small(os.path.join(gitdir, "commondir"))
        common = os.path.normpath(os.path.join(gitdir, common_rel))
        if os.path.normpath(os.path.dirname(gitdir)) != os.path.join(common, "worktrees"):
            return git_root
        back = _read_small(os.path.join(gitdir, "gitdir"))
        back_abs = os.path.realpath(os.path.join(gitdir, back))
        if back_abs != os.path.join(os.path.realpath(git_root), ".git"):
            return git_root
        if os.path.basename(common) != ".git":
            return common   # bare repo
        return os.path.dirname(common)
    except (OSError, UnicodeDecodeError):
        return git_root


# ---------------------------------------------------------------- F6 判定

@dataclass(frozen=True)
class TrustInfo:
    path: str               # 資料夾 realpath
    trusted: bool
    key: str | None         # 讓它受信任的那一筆 projects 鍵
    own: bool               # 信任紀錄就在資料夾本身
    git_root: str | None

    @property
    def inherited(self) -> bool:
        return self.trusted and not self.own


def trust_info(path: str, claude_cfg: dict, home: str) -> TrustInfo:
    p = os.path.realpath(path)
    home_r = os.path.realpath(home)
    projects = projects_of(claude_cfg)

    def accepted(key: str) -> bool:
        if key == home_r:           # 家目錄不存信任
            return False
        ent = projects.get(key)
        return isinstance(ent, dict) and ent.get("hasTrustDialogAccepted") is True

    git_root = find_git_root(p)
    key = canonical_repo_root(git_root) if git_root else p
    if accepted(key):
        return TrustInfo(p, True, key, key == p, git_root)

    cur = p
    while True:
        if cur == home_r or not is_within(cur, home_r):
            break                   # 家目錄與其上層不算
        if git_root is not None and not is_within(cur, git_root):
            break
        if accepted(cur):
            return TrustInfo(p, True, cur, cur == p, git_root)
        if git_root is not None and cur == git_root:
            break                   # 遇到 git 根就停
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return TrustInfo(p, False, None, False, git_root)


def risky_configs(path: str) -> list[str]:
    """資料夾裡會讓 hooks／MCP／指令在 session 啟動時生效的設定。"""
    reasons: list[str] = []
    if os.path.lexists(os.path.join(path, ".mcp.json")):
        reasons.append(".mcp.json（MCP 伺服器設定）")
    for fn in ("settings.json", "settings.local.json"):
        f = os.path.join(path, ".claude", fn)
        if not os.path.lexists(f):
            continue
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            reasons.append(f".claude/{fn}（無法解析，保守視為有風險）")
            continue
        if not isinstance(data, dict):
            reasons.append(f".claude/{fn}（格式異常，保守視為有風險）")
            continue
        keys = [k for k in RISKY_SETTINGS_KEYS if k in data and data[k] not in (None, {}, [], "", False)]
        if keys:
            reasons.append(f".claude/{fn}（含 {', '.join(keys)}）")
    return reasons


def check_open_policy(path: str, claude_cfg: dict, home: str) -> TrustInfo:
    """§5.1 規則 5。通過回傳 TrustInfo，不通過丟 CchubError。"""
    info = trust_info(path, claude_cfg, home)
    if not info.trusted:
        raise CchubError(
            f"「{os.path.basename(info.path)}」還沒受信任（{info.path}）。\n"
            "cchub 不提供信任既有資料夾的能力：請回電腦在那個資料夾執行一次 `claude`，"
            "用官方對話框接受信任，之後就能從手機打開。"
        )
    if info.inherited:
        risky = risky_configs(info.path)
        if risky:
            raise CchubError(
                f"「{os.path.basename(info.path)}」的信任是繼承自上層（{info.key}），"
                "但資料夾裡有會自動生效的設定：\n"
                + "\n".join(f"  - {r}" for r in risky)
                + "\n為了安全，請回電腦在那個資料夾執行一次 `claude`，用官方對話框審閱並信任它，之後再從手機打開。"
            )
    return info


# ---------------------------------------------------------------- §5.2 信任寫入

class ClaudeJsonLock:
    """和 CLI 相容的 mkdir 鎖：<config>.lock。"""

    def __init__(self, config_path: str, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = lambda s: None,
                 timeout: float = LOCK_TIMEOUT, interval: float = LOCK_RETRY_INTERVAL,
                 stale: float = LOCK_STALE_AGE):
        self.lock_dir = config_path + ".lock"
        self.clock, self.wall, self.sleep, self.log = clock, wall, sleep, log
        self.timeout, self.interval, self.stale = timeout, interval, stale
        self.acquired = False

    def __enter__(self) -> "ClaudeJsonLock":
        deadline = self.clock() + self.timeout
        while True:
            try:
                os.mkdir(self.lock_dir, 0o700)
                self.acquired = True
                return self
            except FileExistsError:
                pass
            try:
                st = os.lstat(self.lock_dir)
            except FileNotFoundError:
                continue            # 剛好被放掉，立刻再試
            age = self.wall() - st.st_mtime
            if age > self.stale:
                self.log(f"移除殘留的鎖（mtime 為 {int(age)} 秒前）：{self.lock_dir}")
                try:
                    os.rmdir(self.lock_dir)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    raise CchubError(f"無法移除殘留的鎖 {self.lock_dir}：{e}") from e
                continue
            if self.clock() >= deadline:
                raise CchubError(f"取不到 {self.lock_dir}（等了 {int(self.timeout)} 秒），可能有 Claude Code 正在寫設定，請稍後再試")
            self.sleep(self.interval)

    def __exit__(self, *exc) -> None:
        if self.acquired:
            try:
                os.rmdir(self.lock_dir)
            except FileNotFoundError:
                pass
            self.acquired = False


def backup_claude_json(paths: Paths, raw: bytes, now: float) -> str:
    """備份到 ~/.claude/backups/cchub-claude.json.<時間戳>，只輪替 cchub- 前綴、留 10 份。"""
    ensure_dir(paths.backups_dir, 0o700)
    base = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-%06d" % int((now % 1) * 1_000_000)
    for n in range(100):
        name = BACKUP_PREFIX + (base if n == 0 else f"{base}-{n:02d}")
        dest = os.path.join(paths.backups_dir, name)
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        break
    else:
        raise CchubError("無法建立備份檔（名稱衝突）")
    rotate_backups(paths.backups_dir)
    return dest


def rotate_backups(backups_dir: str, keep: int = BACKUP_KEEP) -> list[str]:
    ours = []
    for name in os.listdir(backups_dir):
        if not name.startswith(BACKUP_PREFIX):
            continue                # 使用者自己的備份（例如 claude.json.20260918-203358.bak）絕不碰
        full = os.path.join(backups_dir, name)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            continue
        ours.append(name)
    ours.sort()
    removed = []
    for name in ours[:-keep] if len(ours) > keep else []:
        os.unlink(os.path.join(backups_dir, name))
        removed.append(name)
    return removed


def verify_new_project(paths: Paths, dir_fd: int, token: str, now: float, expected_path: str,
                       projects_root: str) -> str:
    """§5.2 寫入前的自我驗證（綁 inode，D3）；任一條不成立就丟 TrustRefused。回傳信任鍵。

    - registry 有這次呼叫的暫存紀錄、60 秒內、記錄的資料夾就是 expected_path
    - 用建立時開的目錄 fd 驗證：st_dev/st_ino 與建立時相同；內容（os.listdir(fd)）只有模板
    - 信任鍵＝readlink(/proc/self/fd/<fd>)，必須等於 expected_path、而且直接在 projects_root 底下；
      這個路徑現在指到的也必須是同一個 inode（不是換上去的符號連結）
    """
    registry = read_json(paths.registry_file, default={}) or {}
    pending = (registry.get("pending_new") or {}).get(token)
    if not isinstance(pending, dict):
        raise TrustRefused("registry 裡沒有這次呼叫的暫存紀錄，不寫信任")
    if str(pending.get("dir", "")) != expected_path:
        raise TrustRefused("暫存紀錄的資料夾與目標不符，不寫信任")
    try:
        age = now - float(pending.get("created_at"))
    except (TypeError, ValueError):
        raise TrustRefused("暫存紀錄的時間無效，不寫信任")
    if age < -5 or age > PENDING_MAX_AGE:
        raise TrustRefused(f"暫存紀錄已超過 {int(PENDING_MAX_AGE)} 秒（{int(age)} 秒），不寫信任")
    try:
        st = os.fstat(dir_fd)
    except OSError as e:
        raise TrustRefused(f"讀不到建立時的資料夾：{type(e).__name__}")
    if not stat.S_ISDIR(st.st_mode):
        raise TrustRefused("建立時的資料夾已不是資料夾，不寫信任")
    if (st.st_ino, st.st_dev) != (pending.get("ino"), pending.get("dev")):
        raise TrustRefused("資料夾的 inode 與建立時不同，不寫信任")
    try:
        real = os.readlink(f"/proc/self/fd/{dir_fd}")
    except OSError as e:
        raise TrustRefused(f"查不到資料夾的真實路徑：{type(e).__name__}")
    if real.endswith(" (deleted)"):
        raise TrustRefused("建立時的資料夾已被刪除，不寫信任")
    if real != expected_path or os.path.dirname(real) != projects_root:
        raise TrustRefused("資料夾已被移動或換掉（真實路徑不是剛建立的位置），不寫信任")
    try:
        lst = os.lstat(real)
    except OSError as e:
        raise TrustRefused(f"讀不到資料夾：{type(e).__name__}")
    if not stat.S_ISDIR(lst.st_mode) or (lst.st_ino, lst.st_dev) != (st.st_ino, st.st_dev):
        raise TrustRefused("路徑現在指到的不是剛建立的資料夾，不寫信任")
    entries = sorted(os.listdir(dir_fd))
    extra = [e for e in entries if e not in TEMPLATE_ENTRIES]
    if extra:
        raise TrustRefused(f"資料夾裡有模板以外的東西：{', '.join(extra)}，不寫信任")
    for name in entries:
        est = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        want = TEMPLATE_ENTRIES[name]
        ok = stat.S_ISREG(est.st_mode) if want == "file" else stat.S_ISDIR(est.st_mode)
        if not ok:
            raise TrustRefused(f"{name} 的型別不對（應為{'檔案' if want == 'file' else '資料夾'}，且不能是符號連結），不寫信任")
    return real


def _set_trust(data: dict, key: str, value: bool) -> None:
    projects = data.get("projects")
    if projects is None:
        projects = data["projects"] = {}
    if not isinstance(projects, dict):
        raise CchubError("~/.claude.json 的 projects 不是物件，中止")
    ent = projects.get(key)
    if isinstance(ent, dict):
        ent["hasTrustDialogAccepted"] = value
    elif ent is None:
        if value:
            new = copy.deepcopy(CLI_PROJECT_DEFAULTS)
            new["hasTrustDialogAccepted"] = True
            projects[key] = new
    else:
        raise CchubError(f"~/.claude.json 的 projects[{key}] 格式異常，中止")


def _locked_update(paths: Paths, mutate: Callable[[dict], None], *, wall, clock, sleep, log,
                   precheck: Callable[[], None] | None = None) -> str | None:
    """鎖內：讀 → 解析（失敗中止）→ 備份 → mutate → 暫存檔 0600 → fsync → rename。

    回傳備份路徑；內容沒有變化時不備份也不寫，回傳 None。
    """
    target = os.path.realpath(paths.claude_json)   # ~/.claude.json 若是符號連結，寫到它指的檔
    with ClaudeJsonLock(paths.claude_json, clock=clock, wall=wall, sleep=sleep, log=log):
        if precheck is not None:
            precheck()
        try:
            with open(target, "rb") as f:
                raw = f.read()
        except OSError as e:
            raise CchubError(f"讀不到 {paths.claude_json}：{e}") from e
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise CchubError(f"{paths.claude_json} 解析失敗，中止（沒有寫入）：{e}") from e
        if not isinstance(data, dict):
            raise CchubError(f"{paths.claude_json} 不是 JSON 物件，中止")
        new = copy.deepcopy(data)
        mutate(new)
        if new == data:
            return None
        # 先序列化＋編碼完（D11）：失敗就中止，不備份也不寫
        try:
            payload = dump_json(new).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as e:
            raise CchubError(
                f"{paths.claude_json} 無法重新序列化（{type(e).__name__}，例如含孤立的 surrogate 字元），"
                "中止：沒有寫入、也沒有備份"
            ) from None
        backup = backup_claude_json(paths, raw, wall())
        atomic_write_bytes(target, payload, mode=0o600)
    return backup


def _read_trust_flag(paths: Paths, key: str):
    try:
        data = load_claude_config(os.path.realpath(paths.claude_json))
    except CchubError:
        return None
    ent = projects_of(data).get(key)
    return ent.get("hasTrustDialogAccepted") if isinstance(ent, dict) else None


def grant_trust_for_new_project(paths: Paths, dir_fd: int, token: str, expected_path: str,
                                projects_root: str, *,
                                wall: Callable[[], float] = time.time,
                                clock: Callable[[], float] = time.monotonic,
                                sleep: Callable[[float], None] = time.sleep,
                                log: Callable[[str], None] = lambda s: None) -> str:
    """§5.2：只替 `cchub new` 同一次呼叫剛建立、只有模板的資料夾寫信任。回傳寫入的鍵。

    dir_fd 是 new 建立資料夾後立刻用 O_DIRECTORY|O_NOFOLLOW 開的 fd；信任鍵由它的真實路徑決定（D3）。
    """
    key = verify_new_project(paths, dir_fd, token, wall(), expected_path, projects_root)

    def precheck() -> None:
        if verify_new_project(paths, dir_fd, token, wall(), expected_path, projects_root) != key:
            raise TrustRefused("資料夾路徑在驗證期間變了，不寫信任")

    last_error = None
    for attempt in range(1, READBACK_ATTEMPTS + 1):
        _locked_update(paths, lambda d: _set_trust(d, key, True), wall=wall, clock=clock, sleep=sleep, log=log,
                       precheck=precheck)
        sleep(READBACK_DELAY)
        if _read_trust_flag(paths, key) is True:
            return key
        last_error = f"第 {attempt} 次回讀沒看到信任紀錄"
        log(last_error + "，重試")
    raise CchubError(f"信任寫入後回讀失敗（{last_error}），可能被 Claude Code 同時寫回覆蓋；請稍後重試")


def revoke_trust_keys(paths: Paths, keys: list[str], *, wall=time.time, clock=time.monotonic,
                      sleep=time.sleep, log=lambda s: None) -> list[str]:
    """uninstall 用：把 cchub 加過的信任鍵改回 false（只動 hasTrustDialogAccepted）。"""
    changed: list[str] = []

    def mutate(d: dict) -> None:
        changed.clear()
        projects = projects_of(d)
        for k in keys:
            ent = projects.get(k)
            if isinstance(ent, dict) and ent.get("hasTrustDialogAccepted") is True:
                ent["hasTrustDialogAccepted"] = False
                changed.append(k)

    if keys and os.path.exists(paths.claude_json):
        _locked_update(paths, mutate, wall=wall, clock=clock, sleep=sleep, log=log)
    return changed
