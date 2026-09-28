"""測試共用：暫存家目錄、假的 systemctl／/proc、假的 claude 執行檔。

所有測試只碰 tempfile 建的暫存資料夾；真實的 ~/.claude.json、systemd 一律不碰。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from cchub.cli import Context  # noqa: E402
from cchub.paths import Paths  # noqa: E402
from cchub.procfs import ProcFS, RcProcess  # noqa: E402
from cchub.units import RunResult, Systemctl  # noqa: E402

NOT_IN_UNIT_CGROUP = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-test.scope\n"


def unit_cgroup(unit: str) -> str:
    return f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}\n"


class TempHome:
    """一個假的家目錄。

    - ~/work/projects：projects_root，也是入口（已受信任）；~/work：第二個 allowed_root
    - ~/.config/cchub/config.json：預設寫好（等同已經 install 過）；config=False 表示全新的機器
    - ~/.claude.json、~/.claude/backups
    """

    def __init__(self, config: bool = True) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cchub-test-"))
        self.home = os.path.join(self.tmp, "home")
        self.work = os.path.join(self.home, "work")
        self.project = os.path.join(self.work, "projects")
        os.makedirs(self.project)
        os.makedirs(os.path.join(self.home, ".claude", "backups"))
        self.paths = Paths(self.home)
        if config:
            self.write_config({"projects_root": self.project, "allowed_roots": [self.project, self.work],
                               "entry_dir": self.project})
        self.claude = {
            "numStartups": 42,
            "oauthAccount": {"emailAddress": "someone@example.invalid", "organizationUuid": "fake-org"},
            "remoteDialogSeen": True,
            "projects": {
                self.project: {"allowedTools": [], "hasTrustDialogAccepted": True, "lastCost": 1.5},
                "/somewhere/else": {"allowedTools": ["Bash(ls)"], "hasTrustDialogAccepted": False},
            },
            "中文鍵": "值 ✔︎",
        }
        self.write_claude_json(self.claude)

    def write_config(self, data: dict) -> None:
        os.makedirs(self.paths.config_dir, exist_ok=True)
        with open(self.paths.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def update_config(self, **kw) -> None:
        with open(self.paths.config_file, encoding="utf-8") as f:
            data = json.load(f)
        data.update(kw)
        self.write_config(data)

    def write_claude_json(self, data) -> None:
        with open(self.paths.claude_json, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.chmod(self.paths.claude_json, 0o600)

    def read_claude_json(self) -> dict:
        with open(self.paths.claude_json, encoding="utf-8") as f:
            return json.load(f)

    def mkdir(self, *parts: str) -> str:
        p = os.path.join(self.home, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def write(self, rel: str, text: str) -> str:
        p = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class FakeSystemd:
    """解讀 systemctl／journalctl／systemd-run／loginctl 指令的假 runner。"""

    def __init__(self, on_start=None):
        self.calls: list[list[str]] = []
        self.states: dict[str, str] = {}
        self.enabled: set[str] = set()
        self.on_start = on_start
        self.journal: dict[str, str] = {}

    def runner(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "systemctl":
            args = argv[2:]
            cmd = args[0]
            unit = args[-1]
            if cmd in ("start", "restart"):
                self.states[unit] = "active"
                if self.on_start:
                    self.on_start(unit)
            elif cmd == "stop":
                self.states[unit] = "inactive"
            elif cmd == "reset-failed":
                if self.states.get(unit) == "failed":
                    self.states[unit] = "inactive"
            elif cmd == "show":
                return RunResult(0, self.states.get(unit, "inactive") + "\n")
            elif cmd == "is-enabled":
                return RunResult(0, ("enabled" if unit in self.enabled else "disabled") + "\n")
            elif cmd == "list-units":
                states = next(a.split("=", 1)[1] for a in args if a.startswith("--state=")).split(",")
                lines = [f"{u} loaded {s} running desc" for u, s in sorted(self.states.items())
                         if u.startswith("cchub-rc@") and s in states]
                return RunResult(0, "\n".join(lines) + ("\n" if lines else ""))
            elif cmd in ("enable", "disable"):
                units = [a for a in args[1:] if not a.startswith("--")]
                for u in units:
                    if cmd == "enable":
                        self.enabled.add(u)
                        if "--now" in args:
                            self.states[u] = "active"
                    else:
                        self.enabled.discard(u)
                        if "--now" in args:
                            self.states[u] = "inactive"
            return RunResult(0, "")
        if argv[0] == "journalctl":
            unit = argv[argv.index("-u") + 1]
            return RunResult(0, self.journal.get(unit, ""))
        if argv[0] == "loginctl":
            return RunResult(0, "yes\n")
        return RunResult(0, "")

    def systemctl(self) -> Systemctl:
        return Systemctl(runner=self.runner)

    def cmds(self, name: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "systemctl" and len(c) > 2 and c[2] == name]


class FakeProcFS(ProcFS):
    def __init__(self, cgroup: str = NOT_IN_UNIT_CGROUP, rc=None, exe_deleted=None, pid_cgroups=None):
        super().__init__("/nonexistent-proc")
        self.cgroup = cgroup
        self.rc = list(rc or [])
        self.deleted = dict(exe_deleted or {})
        self.pid_cgroups = dict(pid_cgroups or {})

    def read_cgroup(self, pid="self") -> str:
        if pid == "self":
            return self.cgroup
        return self.pid_cgroups.get(int(pid), "")

    def rc_processes(self):
        return list(self.rc)

    def exe_deleted(self, pid):
        return self.deleted.get(pid)


def rc_proc(pid: int, cwd: str, unit: str | None = None) -> RcProcess:
    cg = unit_cgroup(unit) if unit else NOT_IN_UNIT_CGROUP
    return RcProcess(pid, cwd, ("/x/claude", "remote-control"), cg)


class FakeClock:
    def __init__(self, start: float | None = None):
        self.t = time.time() if start is None else start

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def make_ctx(h: TempHome, sysd: FakeSystemd | None = None, procfs: FakeProcFS | None = None, **kw) -> Context:
    sysd = sysd or FakeSystemd()
    fields = getattr(Context, "__dataclass_fields__", {})
    for opt in ("environ", "stdin"):          # 修正前的版本沒有這兩個欄位（用來對照「修正前會失敗」）
        if opt not in fields:
            kw.pop(opt, None)
    extra = {}
    if "environ" in fields:
        extra["environ"] = kw.pop("environ", {})   # 預設不帶 CLAUDECODE（測試本身可能跑在 Claude Code 裡）
    if "stdin" in fields:
        extra["stdin"] = kw.pop("stdin", io.StringIO(""))
    ctx = Context(
        paths=h.paths,
        systemctl=sysd.systemctl(),
        procfs=procfs or FakeProcFS(),
        out=io.StringIO(),
        err=io.StringIO(),
        sleep=kw.pop("sleep", lambda s: time.sleep(min(s, 0.01))),
        wait_seconds=kw.pop("wait_seconds", 0.5),
        poll_interval=kw.pop("poll_interval", 0.01),
        lock_timeout=kw.pop("lock_timeout", 10.0),
        isatty=kw.pop("isatty", lambda: False),
        **extra,
        **kw,
    )
    ctx.fake = sysd  # type: ignore[attr-defined]
    return ctx


def serve_simulator(h: TempHome, status: str = "ready", error: dict | None = None, recent=None,
                    last_error: dict | None = None, last_error_detail: str | None = None):
    """假裝 _serve：單元一 start 就寫一份狀態檔（跟真的一樣不帶 session 網址，N5）。"""
    from cchub.names import instance_from_unit
    from cchub.units import write_instance_state

    def on_start(unit: str) -> None:
        inst = instance_from_unit(unit)
        if inst is None:
            return
        st = {"status": status, "serve_started_at": time.time(), "error": error,
              "last_error": last_error, "last_error_detail": last_error_detail,
              "env_url": "https://claude.ai/code?environment=env_TEST123",
              "session_urls": [],
              "recent": list(recent or ["12:00:00 [狀態] Ready · x · main"]), "warnings": []}
        write_instance_state(h.paths, inst, st)
    return on_start


FAKE_CLAUDE_SRC = r'''#!{python}
import json, os, signal, sys, time
cfg = json.load(open(os.path.realpath(__file__) + ".json", encoding="utf-8"))
rec = cfg.get("record")
if rec:
    with open(rec, "w", encoding="utf-8") as f:
        json.dump({"argv": sys.argv, "cwd": os.getcwd(), "env_keys": sorted(os.environ)}, f)
def on_term(signum, frame):
    if rec:
        with open(rec + ".term", "w") as f:
            f.write(str(signum))
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
sys.stdout.write(cfg.get("output", ""))
sys.stdout.flush()
if cfg.get("stderr"):
    sys.stderr.write(cfg["stderr"])
    sys.stderr.flush()
deadline = time.time() + float(cfg.get("hang", 0))
while time.time() < deadline:
    time.sleep(0.05)
sys.exit(int(cfg.get("exit", 0)))
'''


def install_fake_claude(h: TempHome, version: str = "2.1.290", *, output: str = "", exit: int = 0,
                        hang: float = 0, stderr: str = "", record: str | None = None,
                        source: str = "native") -> str:
    if source == "native":
        d = os.path.join(h.home, ".local", "share", "claude", "versions")
        path = os.path.join(d, version)
    else:
        d = os.path.join(h.home, ".config", "Claude", "claude-code", version)
        path = os.path.join(d, "claude")
    os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(FAKE_CLAUDE_SRC.replace("{python}", sys.executable))
    os.chmod(path, 0o755)
    with open(path + ".json", "w", encoding="utf-8") as f:
        json.dump({"output": output, "exit": exit, "hang": hang, "stderr": stderr, "record": record}, f)
    return path


SERVE_WRAPPER = r"""
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, {repo!r})
from cchub.paths import Paths
from cchub.serve import run_serve
sys.exit(run_serve(Paths(sys.argv[1]), sys.argv[2]))
"""


def serve_wrapper(h: TempHome) -> str:
    """在子程序裡跑 _serve，用參數注入暫存家目錄（D9：正式程式不讀任何路徑覆寫環境變數）。"""
    path = os.path.join(h.tmp, "run_serve.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(SERVE_WRAPPER.format(repo=REPO))
    return path


def snapshot(root: str) -> dict:
    """root 底下每個檔案／連結的 (型別, 大小, mtime_ns, 連結目標)，用來證明沒被改動。"""
    out = {}
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            p = os.path.join(dp, n)
            st = os.lstat(p)
            out[p] = (st.st_mode, st.st_size, st.st_mtime_ns, os.readlink(p) if os.path.islink(p) else None)
    return out
