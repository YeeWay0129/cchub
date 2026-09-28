"""systemctl 包裝（薄層，測試注入假的）、實例設定與狀態檔讀寫、上限計數、flock。"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .names import instance_from_unit, unit_for_instance
from .paths import Config, Paths, UNIT_PREFIX
from .util import CchubError, atomic_write_json, ensure_dir, read_json

# 算「在跑」的狀態：active、activating（含 auto-restart）、reloading
RUNNING_STATES = ("active", "activating", "reloading")


@dataclass
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], RunResult]


def default_runner(argv: Sequence[str], timeout: float = 60) -> RunResult:
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        return RunResult(127, "", str(e))
    except subprocess.TimeoutExpired:
        return RunResult(124, "", f"逾時：{' '.join(argv)}")
    return RunResult(p.returncode, p.stdout, p.stderr)


def delayed_action_argv(action: str, unit: str, delay: int = 15) -> list[str]:
    """§5.1 規則 9：在 cchub 單元裡對自己動手時，改用延後排程，讓目前這一輪能先回覆。"""
    return ["systemd-run", "--user", f"--on-active={delay}s", "systemctl", "--user", action, unit]


class Systemctl:
    def __init__(self, runner: Runner | None = None):
        self.runner = runner or default_runner

    def run(self, *args: str, check: bool = True) -> RunResult:
        argv = ["systemctl", "--user", *args]
        r = self.runner(argv)
        if check and r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()
            raise CchubError(f"`{' '.join(argv)}` 失敗（exit {r.returncode}）：{msg}")
        return r

    # start／restart 不加 --no-block：等 job 完成（Type=simple 幾乎立刻），
    # 這樣 flock 放掉之前單元已經是 active，下一個呼叫的上限計數才準。
    def start(self, unit: str) -> None:
        self.run("start", unit)

    def stop(self, unit: str) -> None:
        self.run("stop", unit)

    def restart(self, unit: str) -> None:
        self.run("restart", unit)

    def reset_failed(self, unit: str) -> None:
        self.run("reset-failed", unit, check=False)

    def daemon_reload(self) -> None:
        self.run("daemon-reload")

    def enable(self, units: Iterable[str], now: bool = False) -> None:
        self.run("enable", *(["--now"] if now else []), *units)

    def disable(self, units: Iterable[str], now: bool = False) -> None:
        self.run("disable", *(["--now"] if now else []), *units, check=False)

    def is_active(self, unit: str) -> bool:
        return self.active_state(unit) in RUNNING_STATES

    def active_state(self, unit: str) -> str:
        r = self.run("show", "-p", "ActiveState", "--value", unit, check=False)
        return (r.stdout or "").strip() or "unknown"

    def show(self, unit: str, props: Sequence[str]) -> dict[str, str]:
        r = self.run("show", *[a for p in props for a in ("-p", p)], unit, check=False)
        out: dict[str, str] = {}
        for line in (r.stdout or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
        return out

    def is_enabled(self, unit: str) -> str:
        r = self.run("is-enabled", unit, check=False)
        return (r.stdout or "").strip() or "unknown"

    def list_rc_units(self, states: Sequence[str] = RUNNING_STATES) -> list[str]:
        r = self.run("list-units", "--no-legend", "--plain", f"--state={','.join(states)}",
                     f"{UNIT_PREFIX}*.service", check=False)
        units = []
        for line in (r.stdout or "").splitlines():
            tok = line.strip().split(None, 1)
            if tok and tok[0].startswith(UNIT_PREFIX) and tok[0].endswith(".service"):
                units.append(tok[0])
        return units


# ---------------------------------------------------------------- 實例設定與狀態

def read_instance_config(paths: Paths, instance: str) -> dict | None:
    data = read_json(paths.instance_cfg_file(instance), default=None)
    return data if isinstance(data, dict) else None


def write_instance_config(paths: Paths, instance: str, data: dict) -> None:
    ensure_dir(paths.instances_cfg_dir)
    atomic_write_json(paths.instance_cfg_file(instance), data)


def list_instance_configs(paths: Paths) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        names = sorted(os.listdir(paths.instances_cfg_dir))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json") and not n.startswith("."):
            inst = n[:-5]
            try:
                cfg = read_instance_config(paths, inst)
            except CchubError:
                continue
            if cfg:
                out[inst] = cfg
    return out


def read_instance_state(paths: Paths, instance: str) -> dict:
    try:
        data = read_json(paths.instance_state_file(instance), default={})
    except CchubError:
        return {}
    return data if isinstance(data, dict) else {}


def write_instance_state(paths: Paths, instance: str, data: dict) -> None:
    ensure_dir(paths.instances_state_dir)
    atomic_write_json(paths.instance_state_file(instance), data)


def read_registry(paths: Paths) -> dict:
    data = read_json(paths.registry_file, default=None)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise CchubError(f"{paths.registry_file} 格式錯誤")
    data.setdefault("version", 1)
    data.setdefault("projects", {})
    data.setdefault("trust_keys", [])
    data.setdefault("pending_new", {})
    return data


def write_registry(paths: Paths, data: dict) -> None:
    ensure_dir(paths.state_dir)
    atomic_write_json(paths.registry_file, data)


# ---------------------------------------------------------------- 上限

def running_project_units(systemctl: Systemctl, entry_unit: str) -> list[str]:
    return [u for u in systemctl.list_rc_units() if u != entry_unit]


def check_capacity(systemctl: Systemctl, cfg: Config, entry_unit: str, target_unit: str) -> None:
    """§5.1 規則 7：專案伺服器最多 max_servers 個（不含入口），以 systemd 即時狀態計算。"""
    running = [u for u in running_project_units(systemctl, entry_unit) if u != target_unit]
    if len(running) >= cfg.max_servers:
        from .names import systemd_unescape_path
        listing = "\n".join(f"  - {systemd_unescape_path(instance_from_unit(u) or u)}" for u in running)
        raise CchubError(
            f"專案伺服器已達上限 {cfg.max_servers} 個（入口不算），請先關掉一個（cchub stop <名稱>）。目前在跑：\n{listing}"
        )


# ---------------------------------------------------------------- flock

@contextmanager
def state_lock(paths: Paths, timeout: float = 120.0, poll: float = 0.1,
               clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
    """§5.1 規則 7：new、open、stop、restart 外面包一把 flock（<state>/lock），讓並行的呼叫排隊。"""
    ensure_dir(paths.state_dir)
    fd = os.open(paths.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = clock() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock() >= deadline:
                    raise CchubError(f"另一個 cchub 操作一直沒結束（等了 {int(timeout)} 秒），請稍後再試")
                sleep(poll)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def unit_for_dir(path: str) -> str:
    from .names import instance_for_dir
    return unit_for_instance(instance_for_dir(path))
