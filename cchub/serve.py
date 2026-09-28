"""§5.3 `cchub _serve <實例>`：一個薄的 supervisor。

1. 讀實例設定並重新驗證（D10）：實例名＝escape(資料夾)、資料夾在 allowed_roots 內、
   F6 信任成立、§5.1 規則 5 的 open 政策通過；任一不成立 → exit 3 並寫明原因。
2. 上次啟動很快就失敗（例如 401）→ 先在這裡退避再試（10 秒起、最長 5 分鐘、連上就歸零）。
3. Remote Control 的一次性同意沒答（~/.claude.json 的 remoteDialogSeen 不是 true）→ 不啟動，
   記下原因後以一般錯誤結束，交給 systemd 重啟（非永久性）。
4. 等網路（連 api.anthropic.com:443），失敗就在內部指數退避。不用 systemd 的 RestartSteps（F17）。
5. 選版本最高的 claude 執行檔（F14），啟動 `claude remote-control …`（**不加 --verbose**，F24），轉發 SIGTERM。
6. stdout 與 stderr 分開讀：
   - stdout 只接受固定文法的行（D2）：啟動 banner 的環境網址行、連線狀態行（名稱必須是這個資料夾）、
     第一次出現狀態行之前的 session 網址。其他行（session 標題、工具活動…）一律丟棄，也不寫進狀態檔。
   - stderr 只認「完全吻合」的已知 CLI 訊息（D1），其他行只計數、不記內容。
7. 永久性錯誤（exit 3）只看 stderr 裡完全吻合的「未受信任」「未登入」，而且該行必須出現在子程序結束前 30 秒內。
   409 在任何時間點都不算永久性（v0.3.1）；401 也不算。都走一般重啟＋內部退避。寧可多重試，也不要誤判成永久停機。
8. 註冊失敗（N2）：stderr 的 `Error: …` 行後面 5 行內接著 `Exiting in about N seconds.` → 記為非永久性的
   registration_failed，並把那一行 Error（去控制字元、截 200 字）存進狀態檔的 last_error_detail；只存這一行。
   完全吻合的預設 409 原文仍記為 already_served。

CLI 的行為：未受信任、未登入、註冊失敗（例如 409）這類致命錯誤寫到 stderr；
一次性同意提示、狀態顯示、session 清單寫到 stdout。
非互動模式下註冊失敗（409、401…）會先印錯誤、再等 45–75 秒（有 Retry-After 最多 300 秒）才結束。
"""

from __future__ import annotations

import collections
import os
import re
import select
import signal
import socket
import subprocess
import sys
import time
import unicodedata
from typing import Callable, TextIO

from .claudebin import clean_env, select_claude
from .names import check_path_safety, instance_for_dir, unit_for_instance
from .paths import Config, Paths, load_config, validate_mode
from .trust import check_open_policy, find_git_root, load_claude_config, risky_configs, trust_info
from .units import read_instance_config, read_instance_state, write_instance_state
from .util import CchubError

PERMANENT_EXIT = 3
PERM_WINDOW = 30.0          # 永久性錯誤訊息必須出現在子程序結束前 30 秒內
FAST_FAIL_SECONDS = 120.0   # 子程序在這麼短時間內失敗、而且沒上線 → 算一次快速失敗
MAX_LINE = 300
RECENT_KEEP = 200
TERM_GRACE = 15.0           # 轉發 SIGTERM 後等子程序幾秒，逾時就 SIGKILL（單元 TimeoutStopSec=20）

# ---------------------------------------------------------------- 跳脫碼

OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ESC_RE = re.compile(r"\x1b[@-Z\\-_]?")
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
OSC8_TARGET_RE = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")


def strip_ansi(s: str) -> str:
    s = OSC_RE.sub("", s)
    s = CSI_RE.sub("", s)
    s = ESC_RE.sub("", s)
    if "\r" in s:
        segs = [x for x in s.split("\r") if x.strip()]
        s = segs[-1] if segs else ""
    return CTRL_RE.sub("", s)


# ---------------------------------------------------------------- stdout 固定文法（D2）
# CLI 的狀態顯示：狀態列是「<圖示> <狀態字> · <名稱> · <分支>」（從第 0 欄開始）；
# 名稱＝GitHub 遠端的 repo 名，沒有就是 basename(cwd)；非 git 資料夾沒有分支段。
# session 清單、capacity、工具活動都是縮排行 → 一律丟棄。

GLYPH = r"[^\w\s]{1,8}"
STATUS_LINE_RE = re.compile(
    rf"{GLYPH} (?P<label>Ready|Connected|Connecting)(?: · (?P<name>[^·]{{1,100}}?)(?: · (?P<branch>[^\s·]{{1,100}}))?)?"
)
RECONNECTING_RE = re.compile(rf"{GLYPH} Reconnecting · retrying in [0-9a-z. ]{{1,20}} · disconnected [0-9a-z. ]{{1,20}}")
BANNER_RE = re.compile(
    r"(?:Continue coding in the Claude mobile app|Code anywhere with the Claude mobile app) or "
    r"(?P<url>https://claude\.ai/code\?environment=env_[A-Za-z0-9]{1,100})"
)
SESSION_URL_RE = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9]{1,100}")
CONSENT_LINE = "Enable Remote Control? (y/n)"


class StdoutFilter:
    """stdout：只接受固定文法的行；其他一律丟棄（不寫 journal、不寫狀態檔）。"""

    def __init__(self, expected_names: set[str]) -> None:
        self.expected_names = set(expected_names)
        self.seen_status = False
        self.status: str | None = None
        self.status_line: str | None = None
        self._status_key: str | None = None
        self.env_url: str | None = None
        self.session_urls: list[str] = []
        self.consent_seen = False

    def feed(self, raw: str) -> list[str]:
        out: list[str] = []
        line = strip_ansi(raw).rstrip()
        if not line.strip() or line[0].isspace():
            return out                      # 空行、縮排行（session 標題、活動、capacity）一律丟棄
        if not self.seen_status:
            if line == CONSENT_LINE:
                self.consent_seen = True    # 由 supervisor 處理（非永久性），這行本身不寫
                return out
            targets = OSC8_TARGET_RE.findall(raw)
            if SESSION_URL_RE.fullmatch(line):
                targets.append(line)
            for t in targets:
                m = SESSION_URL_RE.match(t)
                if m and m.group(0) not in self.session_urls:
                    self.session_urls.append(m.group(0))
                    del self.session_urls[:-20]
                    out.append(f"[網址] session {m.group(0)}")
        m = STATUS_LINE_RE.fullmatch(line)
        if m:
            label, name, branch = m.group("label"), m.group("name"), m.group("branch")
            if name is None and label != "Connecting":
                return out
            if name is not None and name not in self.expected_names:
                return out                  # 名稱不是這個資料夾 → 可能是 capacity=1 時的 session 標題
            self.seen_status = True
            self.status = "connecting" if label == "Connecting" else "ready"
            norm = label + (f" · {name}" if name else "") + (f" · {branch}" if branch else "")
            if norm != self._status_key:
                self._status_key = norm
                self.status_line = norm
                out.append(f"[狀態] {norm}")
            return out
        if RECONNECTING_RE.fullmatch(line):
            self.seen_status = True
            self.status = "reconnecting"
            if self._status_key != "Reconnecting":
                self._status_key = "Reconnecting"
                self.status_line = "Reconnecting"
                out.append("[狀態] Reconnecting")
            return out
        m = BANNER_RE.fullmatch(line)
        if m:
            if m.group("url") != self.env_url:
                self.env_url = m.group("url")
                out.append(f"[網址] environment {self.env_url}")
            if self.status is None:
                self.status = "ready"
            return out
        return out


# ---------------------------------------------------------------- stderr 已知訊息（D1）

MSG_UNTRUSTED = ("Error: Workspace not trusted. Please run `claude` in {dir} first to review and accept "
                 "the workspace trust dialog.")
MSG_NOT_LOGGED_IN = "Error: You must be logged in to use Remote Control."
MSG_NOT_LOGGED_IN_HINT = ("Remote Control is only available with claude.ai subscriptions. Run `claude auth login` "
                          "to sign in with your claude.ai account.")
MSG_ALREADY_SERVED = "Error: This folder is already served by another Claude Code on this device. Stop it first."
AUTH_401_RE = re.compile(
    r"Error: [A-Za-z ]{1,40}: Authentication failed \(401\)(?:: [^\n]{0,300})?\. Remote Control is only available "
    r"with claude\.ai subscriptions\. Please use `/login` to sign in with your claude\.ai account\."
)
EXITING_RE = re.compile(r"Exiting in about (?P<n>\d{1,4}) seconds?\.")

REASONS = {
    "untrusted": "資料夾未受信任：回電腦在該資料夾執行一次 claude，用官方對話框接受信任",
    "auth": "CLI 未登入：回電腦執行 claude auth login",
    "already_served": "這個資料夾已由其他程序提供（你可能手動開了 claude rc）",
    "registration_failed": "註冊失敗",
    "auth_401": "Remote Control 驗證失敗（401）：CLI 登入可能過期；會自動重試，一直失敗就回電腦執行 claude auth login",
    "consent": "Remote Control 的一次性同意還沒回答：回電腦執行一次 claude remote-control 並回答 y；之後會自動重試",
}
# 409（already_served）在任何時間點都不是永久性錯誤（v0.3.1，N1）
PERMANENT_STDERR_KINDS = ("untrusted", "auth")
PERMANENT_KINDS = PERMANENT_STDERR_KINDS + ("config", "policy", "no_binary")


def match_stderr(line: str, instance_dir: str) -> tuple[str, str | None] | None:
    """完全吻合已知訊息 → (種類, 要寫進 journal 的固定文字或 None)；否則 None。"""
    if line == MSG_UNTRUSTED.format(dir=instance_dir):
        return "untrusted", f"[錯誤] Workspace not trusted（{instance_dir}）"
    if line == MSG_NOT_LOGGED_IN:
        return "auth", "[錯誤] You must be logged in to use Remote Control."
    if line == MSG_NOT_LOGGED_IN_HINT:
        return "auth_hint", None
    if line == MSG_ALREADY_SERVED:
        return "already_served", "[錯誤] This folder is already served by another Claude Code on this device."
    if AUTH_401_RE.fullmatch(line):
        return "auth_401", "[錯誤] Remote Control 驗證失敗（401），會自動重試"
    m = EXITING_RE.fullmatch(line)
    if m:
        return "exiting", f"[訊息] claude 約 {int(m.group('n'))} 秒後結束（註冊失敗後的等待）"
    return None


DETAIL_MAX = 200
REGISTRATION_LOOKBACK = 5   # `Error: …` 之後 5 行內出現 `Exiting in about N seconds.` → 註冊失敗


def sanitize_detail(line: str) -> str:
    """註冊失敗那一行：去掉跳脫碼與控制／格式字元，最多 200 字。"""
    t = "".join(ch for ch in strip_ansi(line) if unicodedata.category(ch) not in ("Cc", "Cf"))
    return t.strip()[:DETAIL_MAX]


class StderrMonitor:
    def __init__(self, instance_dir: str, mono: Callable[[], float]) -> None:
        self.dir = instance_dir
        self.mono = mono
        self.events: list[tuple[float, str]] = []
        self.unknown = 0
        self.detail: str | None = None
        # 最近幾行（只在記憶體裡）：[文字, 時間, 已辨識的種類, 是否算進 unknown]
        self._recent: collections.deque = collections.deque(maxlen=REGISTRATION_LOOKBACK)

    def feed(self, raw: str) -> list[str]:
        line = strip_ansi(raw).rstrip()
        if not line.strip():
            return []
        now = self.mono()
        hit = match_stderr(line, self.dir)
        out: list[str] = []
        if hit is not None and hit[0] == "exiting":
            out.extend(self._registration_failure())
        if hit is None:
            self.unknown += 1               # 不記內容
            self._recent.append([line, now, None, True])
            return out
        kind, text = hit
        if kind != "exiting":
            self.events.append((now, kind))
        self._recent.append([line, now, kind, False])
        if text:
            out.append(text)
        return out

    def _registration_failure(self) -> list[str]:
        """`Exiting in about N seconds.` 之前 5 行內最近的 `Error: …` 行 → 註冊失敗（N2）。"""
        for entry in reversed(self._recent):
            line, t, kind, counted = entry
            if not line.startswith("Error: "):
                continue
            self.detail = sanitize_detail(line)
            if kind is None:                # 還不認得的 Error 行 → registration_failed
                entry[2] = "registration_failed"
                if counted:
                    self.unknown -= 1
                    entry[3] = False
                self.events.append((t, "registration_failed"))
                return [f"[錯誤] 註冊失敗：{self.detail}"]
            return []                       # 已經認得（例如完全吻合的 409、401）：保留原本的種類
        return []

    def last_known_error(self) -> str | None:
        for _, kind in reversed(self.events):
            if kind in REASONS:
                return kind
        return None

    def classify(self, exit_time: float, window: float = PERM_WINDOW) -> tuple[str, str] | None:
        """結束前 window 秒內、完全吻合的永久性訊息 → (kind, 原因)。"""
        # 事件不一定依時間排序（registration_failed 用的是 Error 行當時的時間），所以逐筆比對時間窗
        hits = [(t, kind) for t, kind in self.events
                if kind in PERMANENT_STDERR_KINDS and 0 <= exit_time - t <= window]
        if not hits:
            return None
        kind = max(hits)[1]
        return kind, REASONS[kind]


def reason_text(kind: str | None, detail: str | None) -> str | None:
    if not kind:
        return None
    if kind == "registration_failed":
        return f"註冊失敗：{detail}" if detail else "註冊失敗"
    return REASONS.get(kind, kind)


# ---------------------------------------------------------------- 網路

def tcp_probe(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Backoff:
    """指數退避：10 秒起、每次加倍、最長 5 分鐘；reset() 歸零回到起點。"""

    def __init__(self, initial: float = 10.0, maximum: float = 300.0):
        self.initial, self.maximum = initial, maximum
        self.current = initial
        self.failures = 0

    def next_delay(self) -> float:
        d = self.current
        self.current = min(self.current * 2, self.maximum)
        self.failures += 1
        return d

    def reset(self) -> None:
        self.current = self.initial
        self.failures = 0


def crash_backoff_delay(fast_failures: int, initial: float = 10.0, maximum: float = 300.0) -> float:
    if fast_failures <= 0:
        return 0.0
    return min(initial * (2 ** (fast_failures - 1)), maximum)


def wait_for_network(probe: Callable[[], bool], sleep: Callable[[float], None], backoff: Backoff,
                     on_wait: Callable[[float], None] = lambda d: None) -> int:
    """探測失敗就退避；成功立刻回傳並歸零。回傳失敗次數。"""
    fails = 0
    while not probe():
        d = backoff.next_delay()
        on_wait(d)
        sleep(d)
        fails += 1
    backoff.reset()
    return fails


# ---------------------------------------------------------------- 實例驗證（D10）

class _Terminated(Exception):
    pass


class ServeConfigError(CchubError):
    def __init__(self, message: str, kind: str = "config"):
        super().__init__(message)
        self.kind = kind


TITLE_SAFE_PUNCT = set(" -_.,:;()[]+#@!?&/~%=，。、：；！？（）【】《》〈〉—…·")
TITLE_MAX = 60


def sanitize_title(title: str, fallback: str) -> str:
    """標題清成安全字元集：只留文字、數字、空白與少數標點（去掉引號、控制字元、換行），最多 60 字，開頭不能是 -。"""
    def clean(s: str) -> str:
        kept = []
        for ch in str(s or ""):
            if ch.isalnum() or ch in TITLE_SAFE_PUNCT:
                kept.append(ch)
            elif ch.isspace():
                kept.append(" ")
        t = " ".join("".join(kept).split())[:TITLE_MAX]
        return t.lstrip("-").strip()
    return clean(title) or clean(fallback) or "cchub"


def _github_repo_name(git_root: str) -> str | None:
    """跟 CLI 顯示名稱的規則一致：origin 遠端在 GitHub 時取 repo 名。讀不到就 None。"""
    dotgit = os.path.join(git_root, ".git")
    cfg_path = os.path.join(dotgit, "config")
    try:
        if os.path.isfile(dotgit):
            with open(dotgit, encoding="utf-8") as f:
                content = f.read(4096).strip()
            if content.startswith("gitdir:"):
                gitdir = os.path.normpath(os.path.join(git_root, content[7:].strip()))
                common = gitdir
                cd = os.path.join(gitdir, "commondir")
                if os.path.isfile(cd):
                    with open(cd, encoding="utf-8") as f:
                        common = os.path.normpath(os.path.join(gitdir, f.read(4096).strip()))
                cfg_path = os.path.join(common, "config")
        with open(cfg_path, encoding="utf-8", errors="replace") as f:
            text = f.read(65536)
    except OSError:
        return None
    m = re.search(r'\[remote "origin"\][^\[]*?\burl\s*=\s*(\S+)', text)
    if not m:
        return None
    gm = re.search(r"github\.com[:/]+[^/\s]+/([^/\s]+?)(?:\.git)?/?$", m.group(1))
    return gm.group(1) if gm else None


def expected_status_names(directory: str) -> set[str]:
    names = {os.path.basename(directory)}
    root = find_git_root(directory)
    if root:
        gh = _github_repo_name(root)
        if gh:
            names.add(gh)
    return names


def load_instance(paths: Paths, cfg: Config, instance: str, claude_cfg: dict) -> dict:
    """讀並重新驗證實例設定（D10）。不通過丟 ServeConfigError（→ exit 3）。"""
    icfg = read_instance_config(paths, instance)
    if not icfg:
        raise ServeConfigError(f"找不到實例設定：{paths.instance_cfg_file(instance)}")
    d = icfg.get("dir")
    if not isinstance(d, str) or not os.path.isabs(d) or not os.path.isdir(d):
        raise ServeConfigError(f"實例設定的資料夾無效：{d!r}")
    real = os.path.realpath(d)
    if instance != instance_for_dir(real):
        raise ServeConfigError(f"實例名與設定的資料夾不對應（{instance} ≠ escape({real})）")
    if icfg.get("entry"):
        if real != os.path.realpath(cfg.entry_dir):
            raise ServeConfigError("實例標為入口，但資料夾不是設定裡的 entry_dir")
    else:
        check_path_safety(real, cfg, paths.home)
    info = trust_info(real, claude_cfg, paths.home)
    if not info.trusted:
        raise ServeConfigError(f"資料夾未受信任（F6）：{real}；回電腦在該資料夾執行一次 claude 接受信任", "untrusted")
    if info.inherited and risky_configs(real):
        try:
            check_open_policy(real, claude_cfg, paths.home)
        except CchubError as e:
            raise ServeConfigError(str(e), "policy") from e
    mode = validate_mode(icfg.get("mode", cfg.entry_mode if icfg.get("entry") else cfg.default_mode))
    cap = icfg.get("capacity", cfg.default_capacity)
    if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= 16:
        raise ServeConfigError(f"capacity 無效：{cap!r}")
    return {
        "dir": real,
        "title": sanitize_title(icfg.get("title", ""), os.path.basename(real)),
        "mode": mode,
        "capacity": cap,
        "entry": bool(icfg.get("entry")),
    }


def build_argv(exe: str, inst: dict) -> list[str]:
    # 不加 --verbose（F24）；--continue 不能和 --spawn 併用，普通重啟本來就會接回（§13 誤診 5）
    return [exe, "remote-control", "--name", inst["title"], "--spawn", "same-dir",
            "--capacity", str(inst["capacity"]), "--permission-mode", inst["mode"]]


# ---------------------------------------------------------------- supervisor

class Supervisor:
    def __init__(self, paths: Paths, instance: str, *,
                 probe: Callable[[], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                 wall: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic,
                 out: TextIO | None = None,
                 environ: dict | None = None,
                 install_signals: bool = True,
                 backoff: Backoff | None = None,
                 perm_window: float = PERM_WINDOW):
        self.paths, self.instance = paths, instance
        self.probe, self.sleep, self.popen, self.wall, self.mono = probe, sleep, popen, wall, mono
        self.out = out if out is not None else sys.stdout
        self.environ = dict(os.environ if environ is None else environ)
        self.install_signals = install_signals
        self.backoff = backoff or Backoff()
        self.perm_window = perm_window
        self.stdout_filter: StdoutFilter | None = None
        self.stderr_mon: StderrMonitor | None = None
        self.child: subprocess.Popen | None = None
        self.child_started: float = 0.0
        self.terminating = False
        self._kill_deadline: float | None = None
        self.reached_ready = False
        self._last_error_seen: tuple | None = None
        self.prev_fast_failures = 0
        self.state: dict = {}
        self._dirty = False

    # ------------------------------------------------ 輸出與狀態
    def emit(self, line: str) -> None:
        try:
            self.out.write(line + "\n")
            self.out.flush()
        except (OSError, ValueError):
            pass
        recent = self.state.setdefault("recent", [])
        recent.append(time.strftime("%H:%M:%S", time.localtime(self.wall())) + " " + line)
        del recent[:-RECENT_KEEP]
        self._dirty = True

    def set_state(self, **kw) -> None:
        changed = False
        for k, v in kw.items():
            if self.state.get(k) != v:
                self.state[k] = v
                changed = True
        if changed:
            self.state["last_change"] = self.wall()
            self._dirty = True

    def save(self, force: bool = False) -> None:
        if not (self._dirty or force):
            return
        self.state["updated_at"] = self.wall()
        try:
            write_instance_state(self.paths, self.instance, self.state)
        except OSError as e:
            try:
                self.out.write(f"[cchub] 寫狀態檔失敗：{type(e).__name__}\n")
            except (OSError, ValueError):
                pass
        self._dirty = False

    def _init_state(self) -> None:
        old = read_instance_state(self.paths, self.instance)
        try:
            self.prev_fast_failures = max(0, int(old.get("fast_failures") or 0))
        except (TypeError, ValueError):
            self.prev_fast_failures = 0
        now = self.wall()
        self.state = {
            "instance": self.instance,
            "unit": unit_for_instance(self.instance),
            "status": "starting",
            "serve_pid": os.getpid(),
            "serve_started_at": now,
            "child_pid": None,
            "exe": None,
            "version": None,
            "warnings": [],
            "env_url": None,
            "session_urls": [],
            "status_line": None,
            "error": None,
            # last_error／last_error_detail 只屬於這一輪：沿用上一輪的會讓不相干的新失敗顯示舊原因
            "last_error": None,
            "last_error_detail": None,
            "exit_code": None,
            "fast_failures": self.prev_fast_failures,
            "last_change": now,
            "recent": [str(x) for x in (old.get("recent") or [])][-RECENT_KEEP:],
        }
        self._dirty = True

    def fail_permanent(self, kind: str, reason: str) -> int:
        self.set_state(status="failed", error={"kind": kind, "message": reason, "permanent": True, "at": self.wall()},
                       exit_code=PERMANENT_EXIT)
        self.emit(f"[cchub] 永久性錯誤（{kind}）：{reason}；以 exit 3 結束，systemd 不會重啟")
        self.save(force=True)
        return PERMANENT_EXIT

    def fail_transient(self, kind: str, reason: str, status: str = "exited", code: int = 1) -> int:
        """非永久性錯誤：記下原因、快速失敗次數加一，以一般錯誤結束（systemd 會重啟）。"""
        n = self.prev_fast_failures + 1
        self.set_state(status=status, error={"kind": kind, "message": reason, "permanent": False, "at": self.wall()},
                       exit_code=code, fast_failures=n)
        self.emit(f"[cchub] {reason}（非永久性，下次啟動前會退避 {int(crash_backoff_delay(n))} 秒）")
        self.save(force=True)
        return code

    # ------------------------------------------------ 訊號
    def _on_signal(self, signum, frame) -> None:
        self.terminating = True
        if self.child is None:
            raise _Terminated()
        self._request_child_exit(TERM_GRACE)

    def _request_child_exit(self, grace: float) -> None:
        """送 SIGTERM 給子程序（只送一次）；grace 秒後還沒結束就 SIGKILL。"""
        if self.child is None or self._kill_deadline is not None:
            return
        self._kill_deadline = time.monotonic() + grace
        try:
            self.child.send_signal(signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    # ------------------------------------------------ 主流程
    def run(self) -> int:
        if self.install_signals:
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, self._on_signal)
        self._init_state()
        try:
            return self._run()
        except _Terminated:
            self.set_state(status="stopped")
            self.emit("[cchub] 收到停止訊號，結束")
            self.save(force=True)
            return 0

    def _run(self) -> int:
        try:
            cfg = load_config(self.paths)
        except CchubError as e:
            return self.fail_permanent("config", f"設定無效：{e}")
        try:
            claude_cfg = load_claude_config(self.paths.claude_json)
        except CchubError:
            return self.fail_transient("claude_json", "暫時讀不到 ~/.claude.json，稍後重試")
        try:
            inst = load_instance(self.paths, cfg, self.instance, claude_cfg)
        except ServeConfigError as e:
            return self.fail_permanent(e.kind, f"實例驗證失敗：{e}")
        except CchubError as e:
            return self.fail_permanent("config", f"實例驗證失敗：{e}")
        self.set_state(dir=inst["dir"], mode=inst["mode"], capacity=inst["capacity"], title=inst["title"],
                       entry=inst["entry"])
        self.save(force=True)

        delay = crash_backoff_delay(self.prev_fast_failures)
        if delay:
            self.set_state(status="backoff")
            self.emit(f"[cchub] 前 {self.prev_fast_failures} 次啟動很快就失敗，等 {int(delay)} 秒再試（上線後歸零）")
            self.save()
            self.sleep(delay)

        if claude_cfg.get("remoteDialogSeen") is not True:
            return self.fail_transient("consent", REASONS["consent"], status="needs_consent")

        probe = self.probe or (lambda: tcp_probe(cfg.probe_host, cfg.probe_port))

        def on_wait(d: float) -> None:
            self.set_state(status="waiting_network")
            self.emit(f"[cchub] 連不上 {cfg.probe_host}:{cfg.probe_port}，{int(d)} 秒後重試（網路恢復前不啟動 claude）")
            self.save()

        fails = wait_for_network(probe, self.sleep, self.backoff, on_wait)
        if fails:
            self.emit(f"[cchub] 網路已恢復（重試 {fails} 次後），退避歸零")

        exe, warnings = select_claude(self.paths.native_cli_root, self.paths.desktop_cli_root)
        for w in warnings:
            self.emit(f"[警告] {w}")
        if exe is None:
            return self.fail_permanent("no_binary", warnings[0] if warnings else "找不到 claude 執行檔")
        argv = build_argv(exe.path, inst)
        self.stdout_filter = StdoutFilter(expected_status_names(inst["dir"]))
        self.stderr_mon = StderrMonitor(inst["dir"], self.mono)
        self.set_state(status="starting", exe=exe.path, version=exe.version_str, warnings=warnings)
        self.emit(f"[cchub] 啟動 claude {exe.version_str}（{exe.source}）remote-control：模式 {inst['mode']}、capacity {inst['capacity']}")
        self.save()
        try:
            self.child = self.popen(argv, cwd=inst["dir"], env=clean_env(self.environ),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            return self.fail_transient("spawn", f"無法啟動 claude（{type(e).__name__}）")
        self.child_started = self.mono()
        self.set_state(child_pid=self.child.pid)
        self.save()
        self._pump()
        return self._finish()

    def _after_stdout(self) -> None:
        f = self.stdout_filter
        assert f is not None
        upd = {"env_url": f.env_url, "session_urls": list(f.session_urls), "status_line": f.status_line}
        if f.status:
            upd["status"] = f.status
            if f.status == "ready" and not self.reached_ready:
                self.reached_ready = True
                upd["fast_failures"] = 0     # 上線就歸零
                upd["last_error"] = None     # 上線了，之前的錯誤已經不算數
                upd["last_error_detail"] = None
        self.set_state(**upd)

    def _handle(self, which: str, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace")
        if which == "out":
            assert self.stdout_filter is not None
            for line in self.stdout_filter.feed(text):
                self.emit(line)
            self._after_stdout()
        else:
            assert self.stderr_mon is not None
            for line in self.stderr_mon.feed(text):
                self.emit(line)
            kind = self.stderr_mon.last_known_error()
            if kind and (kind, self.stderr_mon.detail) != self._last_error_seen:
                self._last_error_seen = (kind, self.stderr_mon.detail)
                upd = {"last_error": {"kind": kind, "message": reason_text(kind, self.stderr_mon.detail),
                                      "at": self.wall()}}
                if self.stderr_mon.detail is not None:
                    upd["last_error_detail"] = self.stderr_mon.detail
                self.set_state(**upd)

    def _pump(self) -> None:
        assert self.child is not None and self.child.stdout is not None and self.child.stderr is not None
        fds = {self.child.stdout.fileno(): "out", self.child.stderr.fileno(): "err"}
        bufs = {"out": b"", "err": b""}
        open_fds = set(fds)
        while open_fds:
            if self._kill_deadline is not None and time.monotonic() > self._kill_deadline:
                try:
                    self.child.kill()
                except OSError:
                    pass
                self._kill_deadline = float("inf")
            try:
                ready, _, _ = select.select(list(open_fds), [], [], 1.0)
            except InterruptedError:
                continue
            if not ready:
                if self.child.poll() is not None:
                    break                   # 子程序已結束但管線還被孫程序占著 → 不再等
                continue
            for fd in ready:
                which = fds[fd]
                try:
                    chunk = os.read(fd, 65536)
                except InterruptedError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    open_fds.discard(fd)
                    continue
                buf = bufs[which] + chunk
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    self._handle(which, raw)
                if which == "out" and buf and self.stdout_filter is not None and not self.stdout_filter.seen_status \
                        and strip_ansi(buf.decode("utf-8", "replace")).strip() == CONSENT_LINE:
                    # 同意提示沒有換行，而且 claude 會停在那裡等 stdin → 處理並結束子程序（非永久性）
                    self._handle(which, buf)
                    buf = b""
                if len(buf) > 65536:
                    buf = b""               # 超長而且沒有換行的輸出：不可能是固定文法的行，丟掉
                bufs[which] = buf
            if self.stdout_filter is not None and self.stdout_filter.consent_seen:
                self._request_child_exit(5.0)
            self.save()
        for which, buf in bufs.items():
            if buf:
                self._handle(which, buf)
        self.save()

    def _finish(self) -> int:
        assert self.child is not None
        try:
            rc = self.child.wait(timeout=TERM_GRACE)
        except subprocess.TimeoutExpired:
            self.child.kill()
            rc = self.child.wait()
        exit_time = self.mono()
        for stream in (self.child.stdout, self.child.stderr):
            if stream is not None:
                stream.close()
        assert self.stderr_mon is not None and self.stdout_filter is not None
        if self.stderr_mon.unknown:
            self.emit(f"[cchub] claude 的 stderr 另有 {self.stderr_mon.unknown} 行不在白名單內（內容未記錄）")
        if self.terminating:
            self.set_state(status="stopped", exit_code=rc)
            self.emit(f"[cchub] 已停止（claude 結束碼 {rc}）")
            self.save(force=True)
            return 0
        perm = self.stderr_mon.classify(exit_time, self.perm_window)
        if perm:
            return self.fail_permanent(*perm)
        if self.stdout_filter.consent_seen:
            return self.fail_transient("consent", REASONS["consent"], status="needs_consent")
        code = 1 if rc is None or rc < 0 or rc == PERMANENT_EXIT else rc
        fast = (not self.reached_ready) and code != 0 and (exit_time - self.child_started) < FAST_FAIL_SECONDS
        n = self.prev_fast_failures + 1 if fast else 0
        self.set_state(status="exited", exit_code=rc, fast_failures=n)
        last = self.stderr_mon.last_known_error()
        extra = f"；最後的錯誤：{reason_text(last, self.stderr_mon.detail)}" if last else ""
        self.emit(f"[cchub] claude 結束（結束碼 {rc}），交給 systemd 重啟{extra}")
        self.save(force=True)
        return code


INSTANCE_RE = re.compile(r"[A-Za-z0-9:_.\\-]{1,250}")


def run_serve(paths: Paths, instance: str, **kw) -> int:
    # 實例名只會是 systemd-escape 的結果；不含 /，只擋開頭的 .
    if not INSTANCE_RE.fullmatch(instance or "") or instance.startswith("."):
        print(f"[cchub] 實例名不合法：{instance!r}", file=sys.stderr)
        return PERMANENT_EXIT
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    try:
        return Supervisor(paths, instance, **kw).run()
    except Exception as e:  # noqa: BLE001 — journal 不留可能含子程序輸出的例外訊息
        tb = e.__traceback__
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        where = f"{os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}" if tb else "?"
        print(f"[cchub] _serve 內部錯誤：{type(e).__name__}（{where}）", file=sys.stderr)
        return 1
