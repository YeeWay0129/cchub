"""設定、狀態、實例檔路徑。

所有路徑都從「家目錄」推導。家目錄一律取自系統的使用者資料庫（pwd），
**不讀任何環境變數**（沒有 CCHUB_HOME，也不看 HOME）：
這樣鎖、~/.claude.json、systemd 單元永遠是同一組，不會出現「路徑是假的、systemctl 是真的」。
`env -i` 的最小環境也一樣能用（開機時 systemd 給單元的環境很精簡，不能依賴環境變數）。
測試改用參數注入：Paths(home=暫存資料夾)。
"""

from __future__ import annotations

import os
import pwd
import unicodedata
from dataclasses import dataclass, field

from .util import CchubError, read_json, is_within

# 可接受的權限模式。bypassPermissions 會關掉所有權限確認，手機上就沒有任何把關，所以寫死拒絕
ALLOWED_MODES = ("default", "acceptEdits", "auto", "plan", "dontAsk")
FORBIDDEN_MODES = ("bypassPermissions",)

UNIT_PREFIX = "cchub-rc@"
RECONCILE_SERVICE = "cchub-reconcile.service"
RECONCILE_TIMER = "cchub-reconcile.timer"


def home_dir() -> str:
    """家目錄＝使用者資料庫裡的值；刻意不看環境變數（原因見模組說明）。"""
    return pwd.getpwuid(os.getuid()).pw_dir


@dataclass(frozen=True)
class Paths:
    home: str

    @classmethod
    def default(cls) -> "Paths":
        return cls(home=home_dir())

    def _h(self, *parts: str) -> str:
        return os.path.join(self.home, *parts)

    # --- cchub 自己的設定與狀態 ---
    @property
    def config_dir(self) -> str:
        return self._h(".config", "cchub")

    @property
    def config_file(self) -> str:
        return os.path.join(self.config_dir, "config.json")

    @property
    def instances_cfg_dir(self) -> str:
        return os.path.join(self.config_dir, "instances")

    @property
    def state_dir(self) -> str:
        return self._h(".local", "state", "cchub")

    @property
    def registry_file(self) -> str:
        return os.path.join(self.state_dir, "registry.json")

    @property
    def instances_state_dir(self) -> str:
        return os.path.join(self.state_dir, "instances")

    @property
    def reconcile_state_file(self) -> str:
        return os.path.join(self.state_dir, "reconcile.json")

    @property
    def lock_file(self) -> str:
        return os.path.join(self.state_dir, "lock")

    # --- 安裝位置 ---
    @property
    def install_dir(self) -> str:
        return self._h(".local", "share", "cchub")

    @property
    def install_bin(self) -> str:
        return os.path.join(self.install_dir, "bin", "cchub")

    @property
    def bin_link(self) -> str:
        return self._h(".local", "bin", "cchub")

    @property
    def systemd_user_dir(self) -> str:
        return self._h(".config", "systemd", "user")

    # --- Claude Code 的檔案（只讀，唯一例外是 cchub new 替剛建立的資料夾寫信任，見 trust.py）---
    @property
    def claude_json(self) -> str:
        return self._h(".claude.json")

    @property
    def claude_dir(self) -> str:
        return self._h(".claude")

    @property
    def claude_settings(self) -> str:
        return os.path.join(self.claude_dir, "settings.json")

    @property
    def backups_dir(self) -> str:
        return os.path.join(self.claude_dir, "backups")

    @property
    def skill_dir(self) -> str:
        return os.path.join(self.claude_dir, "skills", "cchub")

    @property
    def skill_file(self) -> str:
        return os.path.join(self.skill_dir, "SKILL.md")

    @property
    def desktop_cli_root(self) -> str:
        return self._h(".config", "Claude", "claude-code")

    @property
    def native_cli_root(self) -> str:
        return self._h(".local", "share", "claude", "versions")

    def unit_path_env(self) -> str:
        """單元的 PATH（開機時 user manager 的 PATH 不含 ~/.local/bin，所以寫進單元）。"""
        return f"{self._h('.local', 'bin')}:/usr/local/bin:/usr/bin:/bin"

    def instance_cfg_file(self, instance: str) -> str:
        return os.path.join(self.instances_cfg_dir, instance + ".json")

    def instance_state_file(self, instance: str) -> str:
        return os.path.join(self.instances_state_dir, instance + ".json")


# 路徑（projects_root／allowed_roots／entry_dir）沒有預設值：第一次 `cchub install --projects-root <目錄>` 時決定，
# 寫進 config.json；之後一律讀 config.json。下面只有非路徑欄位的預設值。
PATH_KEYS = ("projects_root", "allowed_roots", "entry_dir")
DEFAULT_CONFIG = {
    "default_mode": "auto",
    "entry_mode": "auto",
    "default_capacity": 3,
    "max_servers": 6,
    # 以下兩項為 _serve 啟動 claude 前的網路探測目標，可改以便測試
    "probe_host": "api.anthropic.com",
    "probe_port": 443,
}


@dataclass
class Config:
    projects_root: str
    allowed_roots: list[str]
    entry_dir: str
    default_mode: str = "auto"
    entry_mode: str = "auto"
    default_capacity: int = 3
    max_servers: int = 6
    probe_host: str = "api.anthropic.com"
    probe_port: int = 443
    source: str = field(default="(安裝選項)")

    def to_json(self) -> dict:
        return {
            "projects_root": self.projects_root,
            "allowed_roots": list(self.allowed_roots),
            "entry_dir": self.entry_dir,
            "default_mode": self.default_mode,
            "entry_mode": self.entry_mode,
            "default_capacity": self.default_capacity,
            "max_servers": self.max_servers,
            "probe_host": self.probe_host,
            "probe_port": self.probe_port,
        }


def validate_mode(mode: str) -> str:
    """bypassPermissions 寫死拒絕（會關掉所有權限確認）；不在清單上的模式也拒絕。"""
    if mode in FORBIDDEN_MODES:
        raise CchubError(f"權限模式 {mode} 一律拒絕（cchub 不允許 bypass）。可用：{', '.join(ALLOWED_MODES)}")
    if mode not in ALLOWED_MODES:
        raise CchubError(f"不認得的權限模式：{mode}。可用：{', '.join(ALLOWED_MODES)}")
    return mode


def _expand(p: str, home: str) -> str:
    if not isinstance(p, str) or not p or any(unicodedata.category(ch) in ("Cc", "Cf") for ch in p):
        raise CchubError(f"設定中的路徑無效：{p!r}")
    if p == "~":
        p = home
    elif p.startswith("~/"):
        p = os.path.join(home, p[2:])
    if not os.path.isabs(p):
        raise CchubError(f"設定中的路徑必須是絕對路徑：{p}")
    return os.path.normpath(p)


class NotInstalled(CchubError):
    """還沒有 config.json（cchub 尚未安裝）。"""


INSTALL_HINT = ("第一次安裝要指定專案資料夾（新專案建在這裡，也是入口），例如：\n"
                "  cchub install --projects-root ~/projects\n"
                "可以再加 --allowed-root <目錄>（可重複；open 可打開的範圍，預設只有 projects_root）、"
                "--entry-dir <目錄>（入口，預設＝projects_root）。這些目錄都必須已經存在，cchub 不會自動建立。")


def load_config(paths: Paths) -> Config:
    raw = read_json(paths.config_file, default=None)
    if raw is None:
        raise NotInstalled(f"cchub 還沒安裝（找不到 {paths.config_file}）。\n" + INSTALL_HINT)
    return config_from_dict(raw, paths.home, paths.config_file)


def normalize_option_path(p: str, home: str) -> str:
    """install 選項裡的路徑：展開 ~、轉成絕對路徑（相對路徑以目前目錄為準）；含控制字元就拒絕。"""
    if not isinstance(p, str) or not p or any(unicodedata.category(ch) in ("Cc", "Cf") for ch in p):
        raise CchubError(f"路徑無效：{p!r}")
    if p == "~" or p.startswith("~/"):
        p = home if p == "~" else os.path.join(home, p[2:])
    return os.path.normpath(os.path.abspath(p))


def config_from_options(home: str, projects_root: str | None, allowed_roots: list[str] | None = None,
                        entry_dir: str | None = None) -> Config:
    """沒有 config.json 時，由 install 的選項組出設定：allowed_roots 預設 [projects_root]、entry_dir 預設 projects_root。"""
    if not projects_root:
        raise NotInstalled("還沒有 config.json，install 需要 --projects-root。\n" + INSTALL_HINT)

    def norm(p: str) -> str:
        return normalize_option_path(p, home)

    root = norm(projects_root)
    roots = [norm(r) for r in allowed_roots] if allowed_roots else [root]
    entry = norm(entry_dir) if entry_dir else root
    for label, d in [("projects_root", root), ("entry_dir", entry)] + [("allowed_root", r) for r in roots]:
        if not os.path.isdir(d):
            raise CchubError(f"{label} 不存在或不是資料夾：{d}（cchub 不會自動建立，請先建好）")
    raw = {"projects_root": root, "allowed_roots": list(dict.fromkeys(roots)), "entry_dir": entry}
    return config_from_dict(raw, home, "(install 選項)")


def config_from_dict(raw, home: str, source: str) -> Config:
    if not isinstance(raw, dict):
        raise CchubError(f"{source} 格式錯誤（應為 JSON 物件）")
    unknown = set(raw) - set(DEFAULT_CONFIG) - set(PATH_KEYS)
    if unknown:
        raise CchubError(f"{source} 有不認得的欄位：{', '.join(sorted(unknown))}")
    missing = [k for k in PATH_KEYS if k not in raw]
    if missing:
        raise CchubError(f"{source} 缺少必要欄位：{', '.join(missing)}")
    data = dict(DEFAULT_CONFIG)
    data.update(raw)
    home = os.path.normpath(home)
    roots = data["allowed_roots"]
    if not isinstance(roots, list) or not roots:
        raise CchubError("allowed_roots 必須是非空陣列")
    cfg = Config(
        projects_root=_expand(data["projects_root"], home),
        allowed_roots=[_expand(r, home) for r in roots],
        entry_dir=_expand(data["entry_dir"], home),
        default_mode=validate_mode(data["default_mode"]),
        entry_mode=validate_mode(data["entry_mode"]),
        default_capacity=data["default_capacity"],
        max_servers=data["max_servers"],
        probe_host=data["probe_host"],
        probe_port=data["probe_port"],
        source=source,
    )
    if not isinstance(cfg.default_capacity, int) or not 1 <= cfg.default_capacity <= 16:
        raise CchubError("default_capacity 必須是 1–16 的整數")
    if not isinstance(cfg.max_servers, int) or not 1 <= cfg.max_servers <= 20:
        raise CchubError("max_servers 必須是 1–20 的整數")
    if not isinstance(cfg.probe_host, str) or not cfg.probe_host:
        raise CchubError("probe_host 無效")
    if not isinstance(cfg.probe_port, int) or not 1 <= cfg.probe_port <= 65535:
        raise CchubError("probe_port 無效")
    # 保守：允許根目錄一律在家目錄底下、且不是家目錄本身
    for r in cfg.allowed_roots:
        if r == home or not is_within(r, home):
            raise CchubError(f"allowed_roots 只能是家目錄底下的資料夾（不能是家目錄本身）：{r}")
    if not any(is_within(cfg.projects_root, r) for r in cfg.allowed_roots):
        raise CchubError("projects_root 必須在 allowed_roots 之內")
    if not any(is_within(cfg.entry_dir, r) for r in cfg.allowed_roots):
        raise CchubError("entry_dir 必須在 allowed_roots 之內")
    return cfg
