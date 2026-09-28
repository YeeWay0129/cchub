"""cchub CLI。

依賴（systemctl、/proc、時鐘、git、終端機）都放在 Context 裡，測試時注入假的。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import pwd
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, TextIO

from . import __version__
from .claudebin import select_claude
from .install import (ASK_RULES, INSTALL_MARKER, UNIT_TEMPLATES, build_install_plan, build_uninstall_plan,
                      execute_plan, print_plan, templates_dir)
from .names import (has_worktree_component, instance_for_dir, resolve_target, systemd_unescape_path,
                    unit_for_instance, validate_new_name)
from .paths import (Config, NotInstalled, Paths, RECONCILE_TIMER, config_from_options, load_config,
                    normalize_option_path, validate_mode)
from .procfs import ProcFS
from .reconcile import run_reconcile
from .serve import reason_text, run_serve, sanitize_title
from .trust import (check_open_policy, find_git_root, grant_trust_for_new_project, load_claude_config,
                    risky_configs, trust_info)
from .units import (RUNNING_STATES, Systemctl, check_capacity, delayed_action_argv, list_instance_configs,
                    read_instance_config, read_instance_state, read_registry, running_project_units,
                    state_lock, write_instance_config, write_registry)
from .util import CchubError, fmt_duration, is_within, iso_now, read_json

GITIGNORE = """# 由 cchub 建立
.claude/settings.local.json
.env
__pycache__/
node_modules/
.venv/
"""

FALLBACK_PROJECT_TEMPLATE = "# {{title}}\n\n## 初始需求\n\n{{brief}}\n"
PENDING_PRUNE_SECONDS = 3600


def git_init(dir_fd: int) -> None:
    """在 new 建立時開的目錄 fd 裡 git init（cwd＝/proc/self/fd/N），路徑被換掉也不會落到別處。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        r = subprocess.run(["git", "init", "-q"], cwd=f"/proc/self/fd/{dir_fd}", pass_fds=(dir_fd,), env=env,
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CchubError(f"git init 失敗：{type(e).__name__}") from None
    if r.returncode != 0:
        raise CchubError(f"git init 失敗：{(r.stderr or r.stdout).strip()}")


@dataclass
class Context:
    paths: Paths
    systemctl: Systemctl
    procfs: ProcFS
    wall: Callable[[], float] = time.time
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    out: TextIO | None = None
    err: TextIO | None = None
    git: Callable[[str], None] = git_init
    wait_seconds: float = 50.0          # 最多等 50 秒；還沒上線就回報「還在啟動中」，不讓這一輪一直卡著
    poll_interval: float = 1.0
    lock_timeout: float = 120.0
    isatty: Callable[[], bool] = field(default=lambda: sys.stdin.isatty())
    ask: Callable[[str], str] = field(default=input)
    environ: dict | None = None
    stdin: TextIO | None = None

    def __post_init__(self) -> None:
        if self.out is None:
            self.out = sys.stdout
        if self.err is None:
            self.err = sys.stderr
        if self.environ is None:
            self.environ = dict(os.environ)
        if self.stdin is None:
            self.stdin = sys.stdin

    @classmethod
    def default(cls) -> "Context":
        return cls(Paths.default(), Systemctl(), ProcFS())

    def say(self, *lines: str) -> None:
        for line in lines:
            print(line, file=self.out)


# ---------------------------------------------------------------- 共用

def _entry(cfg: Config) -> tuple[str, str, str]:
    real = os.path.realpath(cfg.entry_dir)
    inst = instance_for_dir(real)
    return real, inst, unit_for_instance(inst)


def card_label(path: str) -> str:
    """手機卡片上的標籤取 git root（這是 CLI 的行為，--name 改不了）；不在 git 裡就是資料夾名稱。"""
    root = find_git_root(path)
    return os.path.basename(root or path) or path


def _require_installed(ctx: Context) -> None:
    unit_file = os.path.join(ctx.paths.systemd_user_dir, "cchub-rc@.service")
    if not os.path.exists(unit_file):
        raise CchubError(f"cchub 還沒安裝（找不到 {unit_file}）：請在電腦的終端機執行 cchub install")


@contextmanager
def _lock(ctx: Context):
    with state_lock(ctx.paths, timeout=ctx.lock_timeout, clock=ctx.clock, sleep=ctx.sleep):
        yield


def _run_delayed(ctx: Context, argv: list[str]) -> None:
    r = ctx.systemctl.runner(argv)
    if r.returncode != 0:
        raise CchubError(f"排程失敗（{' '.join(argv)}）：{(r.stderr or r.stdout).strip()}")


def _backup_url(st: dict) -> str | None:
    urls = st.get("session_urls") or []
    return urls[-1] if urls else st.get("env_url")


@dataclass
class WaitResult:
    kind: str           # ready | starting | failed
    state: dict
    active_state: str
    waited: float


def _is_already_served(last: dict, detail) -> bool:
    """資料夾已由其他程序服務：完全吻合的預設 409 原文，或 detail 含「already served」的註冊失敗
    （伺服器回的 409 文字不一定是 CLI 預設的那一句）。"""
    kind = last.get("kind") if isinstance(last, dict) else None
    if kind == "already_served":
        return True
    return kind == "registration_failed" and isinstance(detail, str) and "already served" in detail.lower()


def reason_of(st: dict) -> str | None:
    """狀態檔裡最值得給人看的原因：永久性錯誤優先，其次是上次的錯誤＋detail。"""
    err = st.get("error") if isinstance(st.get("error"), dict) else None
    if err and err.get("message"):
        return str(err["message"])
    last = st.get("last_error") if isinstance(st.get("last_error"), dict) else None
    if last:
        detail = st.get("last_error_detail") if isinstance(st.get("last_error_detail"), str) else None
        return reason_text(last.get("kind"), detail) or last.get("message")
    return None


def wait_ready(ctx: Context, inst: str, unit: str, request_time: float) -> WaitResult:
    """最多等 wait_seconds 秒，結果分 ready／starting／failed。"""
    start = ctx.clock()
    while True:
        st = read_instance_state(ctx.paths, inst)
        fresh = float(st.get("serve_started_at") or 0) >= request_time - 2
        status = st.get("status") if fresh else None
        act = ctx.systemctl.active_state(unit)
        waited = ctx.clock() - start
        if status == "ready":
            return WaitResult("ready", st, act, waited)
        if status in ("failed", "needs_consent"):
            return WaitResult("failed", st, act, waited)
        last = st.get("last_error") if fresh and isinstance(st.get("last_error"), dict) else {}
        if _is_already_served(last, st.get("last_error_detail") if fresh else None):
            # stderr 上的 409（CLI 會再等 45–75 秒才結束）→ 不等它結束，直接視為已由別的程序服務
            return WaitResult("failed", dict(st, error=dict(last, kind="already_served", permanent=False)), act, waited)
        if act in ("failed", "inactive") and (fresh or waited > 3):
            return WaitResult("failed", st if fresh else {}, act, waited)
        if waited >= ctx.wait_seconds:
            return WaitResult("starting", st if fresh else {}, act, waited)
        ctx.sleep(ctx.poll_interval)


def report(ctx: Context, res: WaitResult, *, name: str, label: str, path: str, mode: str,
           capacity: int, unit: str) -> int:
    st = res.state
    lines = [str(x) for x in (st.get("recent") or [])[-10:]]
    if res.kind == "ready":
        # 成功時第一行格式固定：skill 照這一行轉述，告訴使用者到卡片選哪一筆
        ctx.say(f"✅ {name} 已上線：到 Claude App → Code → 這台電腦的卡片 → 選「{label}」開新 session")
        ctx.say(f"資料夾：{path}（權限模式 {mode}，capacity {capacity}）")
        for w in st.get("warnings") or []:
            ctx.say(f"⚠️ {w}")
        url = _backup_url(st)
        if url:
            ctx.say(f"備用網址：{url}")
        return 0
    if res.kind == "starting":
        ctx.say(f"⏳ {name} 還在啟動中（已等 {int(res.waited)} 秒）：稍後到 Claude App → Code → 這台電腦的卡片 → 選「{label}」；"
                "也可以說「現在開著哪些」（cchub ls）查看")
        if lines:
            ctx.say("最後紀錄（已過濾）：", *("  " + x for x in lines))
        url = _backup_url(st)
        if url:
            ctx.say(f"備用網址：{url}")
        return 0
    msg = reason_of(st) or f"伺服器沒有起來（systemd 狀態：{res.active_state}）"
    ctx.say(f"❌ {name} 啟動失敗：{msg}")
    if lines:
        ctx.say("最後紀錄（已過濾）：", *("  " + x for x in lines))
    else:
        ctx.say(f"（在電腦上可用 journalctl --user -u '{unit}' 查看）")
    return 1


def _status_text(act: str, st: dict) -> str:
    reason = reason_of(st)
    if act in RUNNING_STATES:
        s = st.get("status")
        return {
            "ready": "✅ 上線",
            "reconnecting": "🔄 重新連線中",
            "waiting_network": "⏳ 等網路",
            "exited": "⏳ 重啟中",
            "disconnected": "⚠️ 已斷線",
        }.get(s, "⏳ 啟動中")
    if act == "failed":
        return "❌ 失敗" + (f"：{reason}" if reason else "")
    if act == "inactive":
        return "⛔ 停止" + (f"：{reason}" if reason else "")
    return act


# ---------------------------------------------------------------- ls

def cmd_ls(ctx: Context, args) -> int:
    p = ctx.paths
    cfg = load_config(p)
    entry_real, entry_inst, entry_unit = _entry(cfg)
    configs = list_instance_configs(p)
    insts = [entry_inst] + sorted(i for i in configs if i != entry_inst)
    rows = []
    for inst in insts:
        icfg = configs.get(inst) or {}
        is_entry = inst == entry_inst
        d = icfg.get("dir") or (entry_real if is_entry else systemd_unescape_path(inst))
        unit = unit_for_instance(inst)
        act = ctx.systemctl.active_state(unit)
        st = read_instance_state(p, inst)
        running = act in RUNNING_STATES
        since = st.get("serve_started_at") if running else None
        rows.append({
            "name": os.path.basename(d) or d,
            "entry": is_entry,
            "installed": bool(icfg),
            "dir": d,
            "label": card_label(d) if os.path.isdir(d) else os.path.basename(d),
            "unit": unit,
            "active_state": act,
            "status": st.get("status"),
            "status_text": _status_text(act, st),
            "uptime_seconds": int(ctx.wall() - float(since)) if since else None,
            "mode": icfg.get("mode"),
            "capacity": icfg.get("capacity"),
            "status_line": st.get("status_line"),
            "last_error": st.get("last_error"),
            "last_error_detail": st.get("last_error_detail"),
            "reason": reason_of(st),
            "env_url": st.get("env_url"),
            "session_urls": st.get("session_urls") or [],
            "warnings": st.get("warnings") or [],
            "error": st.get("error"),
        })
    if args.json:
        ctx.say(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    n_proj = len(running_project_units(ctx.systemctl, entry_unit))
    ctx.say(f"專案伺服器 {n_proj}／{cfg.max_servers}（入口不算）")
    for r in rows:
        name = f"{r['name']}（入口）" if r["entry"] else r["name"]
        up = f"，已上線 {fmt_duration(r['uptime_seconds'])}" if r["uptime_seconds"] is not None and r["status"] == "ready" else ""
        extra = f"，{r['mode']} ×{r['capacity']}" if r["mode"] else ""
        if r["entry"] and not r["installed"]:
            extra += "，尚未安裝（cchub install）"
        ctx.say(f"{r['status_text']}  {name}{up}{extra}")
        ctx.say(f"    資料夾：{r['dir']}（卡片：{r['label']}）")
        if r["active_state"] in RUNNING_STATES and r["status"] != "ready" and r["reason"]:
            ctx.say(f"    上次錯誤：{r['reason']}")
        for w in r["warnings"]:
            ctx.say(f"    ⚠️ {w}")
        url = _backup_url({"session_urls": r["session_urls"], "env_url": r["env_url"]})
        if url and r["active_state"] in RUNNING_STATES:
            ctx.say(f"    網址：{url}")
    return 0


# ---------------------------------------------------------------- open

def _instance_record(path: str, title: str, mode: str, capacity: int, created_by: str, now: float) -> dict:
    return {"dir": path, "title": title, "mode": mode, "capacity": capacity, "entry": False,
            "created_by": created_by, "created_at": iso_now(now)}


def cmd_open(ctx: Context, args) -> int:
    p = ctx.paths
    cfg = load_config(p)
    t = resolve_target(args.target, cfg, p.home)
    entry_real, entry_inst, entry_unit = _entry(cfg)
    if t.is_entry:
        act = ctx.systemctl.active_state(entry_unit)
        ctx.say(f"入口（{t.path}）是常駐的，不用 open：手機卡片上選「{card_label(t.path)}」即可（目前 systemd 狀態：{act}）")
        return 0
    mode = validate_mode(args.mode or cfg.default_mode)
    check_open_policy(t.path, load_claude_config(p.claude_json), p.home)
    _require_installed(ctx)
    inst = instance_for_dir(t.path)
    unit = unit_for_instance(inst)
    label = card_label(t.path)
    with _lock(ctx):
        if ctx.systemctl.is_active(unit):
            st = read_instance_state(p, inst)
            ctx.say(f"✅ {t.name} 已上線：到 Claude App → Code → 這台電腦的卡片 → 選「{label}」開新 session（之前就由 cchub 起好了）")
            url = _backup_url(st)
            if url:
                ctx.say(f"備用網址：{url}")
            return 0
        foreign = ctx.procfs.foreign_rc_for(t.path)
        if foreign:
            pids = ", ".join(str(x.pid) for x in foreign)
            ctx.say(f"「{t.name}」已由其他程序提供 Remote Control（pid {pids}）：直接在手機卡片上選「{label}」即可，cchub 不另外起")
            return 0
        check_capacity(ctx.systemctl, cfg, entry_unit, unit)
        write_instance_config(p, inst, _instance_record(t.path, sanitize_title(t.name, t.name), mode,
                                                        cfg.default_capacity, "open", ctx.wall()))
        request_time = ctx.wall()
        ctx.systemctl.reset_failed(unit)
        ctx.systemctl.start(unit)
    res = wait_ready(ctx, inst, unit, request_time)
    if res.kind == "failed" and (res.state.get("error") or {}).get("kind") == "already_served":
        ctx.systemctl.stop(unit)
        ctx.say(f"「{t.name}」已由其他程序提供 Remote Control（啟動後收到 409）：直接在手機卡片上選「{label}」即可；"
                "剛起的單元已停掉（若是剛停掉或當機後馬上重開，稍等一分鐘再 open 一次）")
        return 0
    return report(ctx, res, name=t.name, label=label, path=t.path, mode=mode, capacity=cfg.default_capacity, unit=unit)


# ---------------------------------------------------------------- new

def _registry_update(ctx: Context, fn: Callable[[dict], None]) -> None:
    reg = read_registry(ctx.paths)
    fn(reg)
    write_registry(ctx.paths, reg)


def _render_project_md(*, name: str, title: str, brief: str, git: bool, now: float, path: str) -> str:
    try:
        with open(os.path.join(templates_dir(), "project-CLAUDE.md"), "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = FALLBACK_PROJECT_TEMPLATE
    values = {
        "title": title,
        "name": name,
        "dir": path,
        "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
        "brief": brief or "（建立時沒有提供需求；開始前先問使用者要做什麼。）",
        "git_note": "這個資料夾有 git：完成一個段落就小步提交。" if git else "這個資料夾沒有 git（建立時用了 --no-git）。",
    }
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), text)


def _write_new_file(dir_fd: int, name: str, text: str) -> None:
    """在建立時開的目錄 fd 裡新建檔案（O_EXCL|O_NOFOLLOW），路徑被換掉也寫不到別處。"""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


BRIEF_MAX = 20000


def _read_brief(ctx: Context, args) -> str:
    """需求只能從 stdin 傳入（--brief-stdin）：原話不經過 shell 的參數展開。"""
    text = ""
    if args.brief_stdin:
        assert ctx.stdin is not None
        text = ctx.stdin.read(BRIEF_MAX + 1)
    text = text.strip()
    if len(text) > BRIEF_MAX:
        raise CchubError(f"需求太長（上限 {BRIEF_MAX} 字）")
    return text


def cmd_new(ctx: Context, args) -> int:
    p = ctx.paths
    cfg = load_config(p)
    name = validate_new_name(args.name)
    mode = validate_mode(args.mode or cfg.default_mode)
    title = sanitize_title(args.title or "", name)
    brief = _read_brief(ctx, args)
    root = os.path.realpath(cfg.projects_root)
    if not os.path.isdir(root):
        raise CchubError(f"projects_root 不存在：{root}")
    target = os.path.join(root, name)
    if has_worktree_component(target) or not any(is_within(target, os.path.realpath(r)) for r in cfg.allowed_roots):
        raise CchubError(f"新專案位置不在允許範圍內：{target}")
    inst = instance_for_dir(target)
    unit = unit_for_instance(inst)
    _, _, entry_unit = _entry(cfg)
    use_git = not args.no_git
    trust_key = None
    _require_installed(ctx)
    with _lock(ctx):
        if os.path.lexists(target):
            raise CchubError(f"「{name}」已存在：{target}（new 只建新資料夾；既有的請用 cchub open {name}）")
        check_capacity(ctx.systemctl, cfg, entry_unit, unit)
        token = uuid.uuid4().hex
        now = ctx.wall()

        def add_pending(reg: dict) -> None:
            pend = reg["pending_new"]
            for k in [k for k, v in pend.items()
                      if not isinstance(v, dict) or now - float(v.get("created_at") or 0) > PENDING_PRUNE_SECONDS]:
                del pend[k]
            pend[token] = {"dir": target, "created_at": now, "pid": os.getpid()}

        _registry_update(ctx, add_pending)
        dir_fd = -1
        try:
            try:
                os.mkdir(target, 0o755)
            except FileExistsError:
                raise CchubError(f"「{name}」已存在：{target}")
            # 建好立刻開 fd（O_NOFOLLOW），之後的寫檔、git init、信任驗證都綁在這個 inode 上：
            # 路徑中途被換成 symlink 或改名，也不會寫到、信任到別的資料夾
            dir_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            st = os.fstat(dir_fd)
            _registry_update(ctx, lambda reg: reg["pending_new"][token].update(ino=st.st_ino, dev=st.st_dev))
            _write_new_file(dir_fd, "CLAUDE.md",
                            _render_project_md(name=name, title=title, brief=brief, git=use_git, now=now, path=target))
            if use_git:
                _write_new_file(dir_fd, ".gitignore", GITIGNORE)
                ctx.git(dir_fd)
                # 只替這次呼叫剛建立、只有模板的資料夾寫信任：
                # 空資料夾沒有 hooks、MCP 或允許規則，信任它不會讓任何既有設定生效
                trust_key = grant_trust_for_new_project(p, dir_fd, token, target, root, wall=ctx.wall,
                                                        clock=ctx.clock, sleep=ctx.sleep,
                                                        log=lambda s: print(f"  {s}", file=ctx.err))
        finally:
            if dir_fd >= 0:
                os.close(dir_fd)
            _registry_update(ctx, lambda reg: reg["pending_new"].pop(token, None))

        def record(reg: dict) -> None:
            reg["projects"][target] = {"created_by": "cchub new", "created_at": iso_now(now), "git": use_git,
                                       "title": title}
            if trust_key and trust_key not in reg["trust_keys"]:
                reg["trust_keys"].append(trust_key)

        _registry_update(ctx, record)
        if not use_git:
            info = trust_info(target, load_claude_config(p.claude_json), p.home)
            if not info.trusted:
                raise CchubError(
                    f"已建立 {target}（--no-git，不寫信任），但它沒有繼承到信任，所以沒有起伺服器。"
                    f"請回電腦在那裡執行一次 claude 接受信任，之後用 cchub open {name}"
                )
        write_instance_config(p, inst, _instance_record(target, title, mode, cfg.default_capacity, "new", now))
        request_time = ctx.wall()
        ctx.systemctl.reset_failed(unit)
        ctx.systemctl.start(unit)
    res = wait_ready(ctx, inst, unit, request_time)
    if res.kind == "failed" and (res.state.get("error") or {}).get("kind") == "untrusted":
        # 生效驗證：寫了信任，CLI 卻仍說未受信任（例如 CLI 改了信任的存法）→ 回報並停掉單元
        ctx.systemctl.stop(unit)
        ctx.say(f"❌ {name} 的信任寫入沒有生效（CLI 仍回報 Workspace not trusted），已停掉伺服器。"
                f"資料夾已建立在 {target}；請回電腦在那裡執行一次 claude 接受信任，之後用 cchub open {name}")
        return 1
    return report(ctx, res, name=name, label=name, path=target, mode=mode, capacity=cfg.default_capacity, unit=unit)


# ---------------------------------------------------------------- stop / restart / logs

def cmd_stop(ctx: Context, args) -> int:
    p = ctx.paths
    cfg = load_config(p)
    t = resolve_target(args.target, cfg, p.home)
    if t.is_entry:
        raise CchubError("入口不能停止：停了之後手機就沒有入口。要重啟入口請用 cchub restart entry")
    inst = instance_for_dir(t.path)
    unit = unit_for_instance(inst)
    with _lock(ctx):
        act = ctx.systemctl.active_state(unit)
        if act not in RUNNING_STATES:
            if act == "failed":
                ctx.systemctl.reset_failed(unit)
            ctx.say(f"「{t.name}」沒有在跑（{act}）")
            return 0
        if ctx.procfs.current_unit() == unit:
            _run_delayed(ctx, delayed_action_argv("stop", unit))
            ctx.say(f"⏳ 已排程：15 秒後停止「{t.name}」。你正在這個伺服器的 session 裡，先讓這一輪回覆完。")
            return 0
        ctx.systemctl.stop(unit)
    ctx.say(f"⛔ {t.name} 已停止（{t.path}）")
    return 0


def cmd_restart(ctx: Context, args) -> int:
    p = ctx.paths
    cfg = load_config(p)
    t = resolve_target(args.target, cfg, p.home)
    _, entry_inst, entry_unit = _entry(cfg)
    inst = entry_inst if t.is_entry else instance_for_dir(t.path)
    unit = unit_for_instance(inst)
    icfg = read_instance_config(p, inst)
    if icfg is None:
        if t.is_entry:
            raise CchubError("入口還沒安裝（找不到實例設定）：請在電腦的終端機執行 cchub install")
        raise CchubError(f"「{t.name}」還沒由 cchub 管理：請用 cchub open {t.name}")
    label = card_label(t.path)
    _require_installed(ctx)
    with _lock(ctx):
        # 在 cchub 單元裡重啟入口（或自己所在的伺服器）→ 延後 15 秒排程：立刻重啟會砍掉正在回覆的這個 session
        if (t.is_entry and ctx.procfs.in_cchub_unit()) or ctx.procfs.current_unit() == unit:
            _run_delayed(ctx, delayed_action_argv("restart", unit))
            ctx.say(f"⏳ 已排程：15 秒後重啟「{label}」。你正在 cchub 管理的 session 裡，先讓這一輪回覆完；"
                    "重啟後會接回原本的 session")
            return 0
        if not t.is_entry:
            check_open_policy(t.path, load_claude_config(p.claude_json), p.home)
            if not ctx.systemctl.is_active(unit):
                check_capacity(ctx.systemctl, cfg, entry_unit, unit)
        request_time = ctx.wall()
        ctx.systemctl.reset_failed(unit)
        ctx.systemctl.restart(unit)
    res = wait_ready(ctx, inst, unit, request_time)
    return report(ctx, res, name=t.name, label=label, path=t.path, mode=str(icfg.get("mode")),
                  capacity=int(icfg.get("capacity") or cfg.default_capacity), unit=unit)


def cmd_logs(ctx: Context, args) -> int:
    p = ctx.paths
    cfg = load_config(p)
    t = resolve_target(args.target, cfg, p.home)
    inst = _entry(cfg)[1] if t.is_entry else instance_for_dir(t.path)
    unit = unit_for_instance(inst)
    n = max(1, min(int(args.n), 1000))
    r = ctx.systemctl.runner(["journalctl", "--user", "-u", unit, "-n", str(n), "--no-pager", "-o", "short-iso"])
    lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip() and not ln.startswith("-- ")]
    if r.returncode == 0 and lines:
        ctx.say(f"「{t.name}」最近 {len(lines)} 行（journal；_serve 已用白名單過濾，不含對話內容）：", *lines)
        return 0
    recent = (read_instance_state(p, inst).get("recent") or [])[-n:]
    if not recent:
        ctx.say(f"「{t.name}」沒有紀錄")
        return 0
    ctx.say(f"「{t.name}」最近 {len(recent)} 行（狀態檔，已過濾）：", *recent)
    return 0


# ---------------------------------------------------------------- doctor

def cmd_doctor(ctx: Context, args=None) -> int:
    p = ctx.paths
    items: list[tuple[str, str]] = []
    icon = {"ok": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️"}

    def add(level: str, text: str) -> None:
        items.append((level, text))

    def flush() -> None:
        ctx.say("cchub doctor")
        for level, text in items:
            ctx.say(f"{icon[level]} {text}")

    try:
        cfg = load_config(p)
        add("ok", f"設定：{cfg.source}（projects_root：{cfg.projects_root}）")
    except NotInstalled as e:
        add("fail", str(e))
        flush()
        return 1
    except CchubError as e:
        add("fail", f"設定錯誤：{e}")
        flush()
        return 1
    entry_real, entry_inst, entry_unit = _entry(cfg)

    add("ok" if sys.version_info >= (3, 10) else "fail", f"Python {platform.python_version()}")
    for tool in ("systemctl", "journalctl", "systemd-run", "git"):
        add("ok" if shutil.which(tool) else "fail", f"{tool}：{shutil.which(tool) or '找不到'}")
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
        r = ctx.systemctl.runner(["loginctl", "show-user", user, "-p", "Linger", "--value"])
        linger = (r.stdout or "").strip()
        add("ok" if linger == "yes" else "warn", f"Linger={linger or '未知'}（開機未登入桌面時也要起入口）")
    except (KeyError, OSError):
        add("warn", "無法查 Linger")

    exe, warns = select_claude(p.native_cli_root, p.desktop_cli_root)
    if exe:
        add("ok", f"claude {exe.version_str}（{exe.path}）")
    for w in warns:
        add("warn" if exe else "fail", w)

    try:
        cj = load_claude_config(p.claude_json)
    except CchubError as e:
        cj = {}
        add("fail", str(e))
    add("ok" if cj.get("remoteDialogSeen") is True else "fail",
        "Remote Control 一次性同意：" + ("已回答" if cj.get("remoteDialogSeen") is True
                                      else "還沒回答 → 在電腦上執行一次 claude remote-control 並回答 y"))
    add("info", "CLI 登入是否過期無法事前偵測（claude auth status 沒有到期資訊）；失效時伺服器會以 auth 錯誤停下，cchub ls 會顯示")
    ti = trust_info(entry_real, cj, p.home)
    add("ok" if ti.trusted else "fail", f"入口資料夾 {entry_real}：" + ("受信任" if ti.trusted else "未受信任"))

    # 安裝與防護狀態
    marker = os.path.join(p.install_dir, INSTALL_MARKER)
    if os.path.exists(marker):
        inside = any(is_within(os.path.realpath(p.install_dir), os.path.realpath(r)) for r in cfg.allowed_roots)
        add("fail" if inside else "ok", f"安裝目錄 {p.install_dir}" + ("（在允許根目錄內！入口可以改寫它）" if inside else ""))
    else:
        add("warn", f"尚未安裝到 {p.install_dir}（cchub install）")
    link_ok = os.path.islink(p.bin_link) and os.readlink(p.bin_link) == p.install_bin
    add("ok" if link_ok else "warn", f"{p.bin_link} → {p.install_bin}" + ("" if link_ok else "：不存在或指向別處"))
    for n in UNIT_TEMPLATES:
        f = os.path.join(p.systemd_user_dir, n)
        add("ok" if os.path.exists(f) else "warn", f"單元檔 {f}" + ("" if os.path.exists(f) else "：不存在"))
    add("ok" if os.path.exists(p.skill_file) else "warn", f"skill {p.skill_file}" + ("" if os.path.exists(p.skill_file) else "：不存在"))
    try:
        settings = read_json(p.claude_settings, default={}) or {}
        ask = ((settings.get("permissions") or {}).get("ask") or []) if isinstance(settings, dict) else []
        missing = [r for r in ASK_RULES if r not in ask]
        add("ok" if not missing else "warn",
            "使用者層級 ask 規則（體驗層，不是安全邊界）：" + ("齊全" if not missing else "缺 " + "、".join(missing)))
    except CchubError as e:
        add("warn", f"讀不到 {p.claude_settings}：{e}")
    add("ok" if ctx.systemctl.is_enabled(RECONCILE_TIMER) == "enabled" else "warn",
        f"{RECONCILE_TIMER}：{ctx.systemctl.is_enabled(RECONCILE_TIMER)}／{ctx.systemctl.active_state(RECONCILE_TIMER)}")
    act = ctx.systemctl.active_state(entry_unit)
    est = read_instance_state(p, entry_inst)
    add("ok" if act in RUNNING_STATES else "warn", f"入口 {entry_unit}：{_status_text(act, est)}")

    running = running_project_units(ctx.systemctl, entry_unit)
    add("ok" if len(running) < cfg.max_servers else "warn", f"專案伺服器 {len(running)}／{cfg.max_servers}")
    for inst, _ in list_instance_configs(p).items():
        st = read_instance_state(p, inst)
        err = st.get("error") if isinstance(st.get("error"), dict) else None
        if err and st.get("status") == "failed":
            add("fail", f"{systemd_unescape_path(inst)}：{err.get('message')}")
    cur = ctx.procfs.current_unit()
    add("info", f"目前在 cchub 單元裡：{cur}" if cur else "目前不在 cchub 單元裡（電腦終端機或其他 session）")
    for proc in ctx.procfs.rc_processes():
        if proc.cchub_unit is None:
            add("warn", f"手動開的 Remote Control：pid {proc.pid}，資料夾 {proc.cwd}（上線前請 Ctrl+C 關掉：同一個資料夾只能由一個伺服器服務）")
    flush()

    untrusted, risky = [], []
    try:
        children = sorted(os.listdir(cfg.projects_root))
    except OSError:
        children = []
    for n in children:
        d = os.path.join(cfg.projects_root, n)
        if n.startswith(".") or not os.path.isdir(d):
            continue
        info = trust_info(d, cj, p.home)
        if not info.trusted:
            untrusted.append(d)
        elif info.inherited:
            rs = risky_configs(info.path)
            if rs:
                risky.append((d, rs))
    ctx.say("")
    if untrusted:
        ctx.say("未受信任的專案（open 會拒絕；要回電腦在該資料夾執行一次 claude 並接受信任）：",
                *(f"  - {d}" for d in untrusted))
    else:
        ctx.say(f"{cfg.projects_root} 底下的專案都受信任")
    if risky:
        ctx.say("信任繼承自上層、但含 hooks／MCP 等設定（open 會拒絕，要回電腦用官方對話框信任一次）：")
        for d, rs in risky:
            ctx.say(f"  - {d}：{'；'.join(rs)}")
    return 1 if any(level == "fail" for level, _ in items) else 0


# ---------------------------------------------------------------- install / uninstall

def _refuse_in_unit(ctx: Context, what: str) -> None:
    if ctx.procfs.in_cchub_unit():
        raise CchubError(f"{what} 只能在電腦的終端機執行：偵測到在 cchub 管理的單元裡（手機的 session），拒絕")


def _require_own_terminal(ctx: Context, what: str) -> None:
    """真的安裝／移除：必須是你自己的終端機。

    - 在 Claude Code session 的 Bash 裡（有 CLAUDECODE 環境變數）一律拒絕
    - stdin 必須是 TTY，而且要手動輸入 yes（沒有可以跳過確認的旗標）
    這是深度防禦：用 script 造假 TTY、或清掉環境變數仍然繞得過，所以 ask 規則與這些檢查都只是體驗層。
    """
    assert ctx.environ is not None
    if "CLAUDECODE" in ctx.environ:
        raise CchubError(f"{what} 要在你自己的終端機執行：偵測到正在 Claude Code session 裡（CLAUDECODE），拒絕")
    if not ctx.isatty():
        raise CchubError(f"{what} 只能在電腦的終端機互動執行（stdin 不是終端機）")


def _install_config(ctx: Context, args) -> Config:
    """install 用的設定：已有 config.json → 一律沿用（行為不變）；沒有 → 由 --projects-root 等選項組出。

    已有 config.json 又給了跟它不同的選項 → 拒絕（不默默覆蓋、也不默默忽略）。
    """
    opts = {"projects_root": args.projects_root, "allowed_roots": args.allowed_root, "entry_dir": args.entry_dir}
    if not os.path.exists(ctx.paths.config_file):
        return config_from_options(ctx.paths.home, **opts)
    cfg = load_config(ctx.paths)
    if any(v for v in opts.values()):
        # 只比對有給的選項（正規化後），不先做其他驗證，確保不一致時一定看到這個說明
        home = ctx.paths.home
        diffs = []
        if args.projects_root and normalize_option_path(args.projects_root, home) != cfg.projects_root:
            diffs.append(f"projects_root：既有 {cfg.projects_root}，選項 {normalize_option_path(args.projects_root, home)}")
        if args.allowed_root and {normalize_option_path(r, home) for r in args.allowed_root} != set(cfg.allowed_roots):
            diffs.append(f"allowed_roots：既有 {cfg.allowed_roots}，選項 "
                         f"{[normalize_option_path(r, home) for r in args.allowed_root]}")
        if args.entry_dir and normalize_option_path(args.entry_dir, home) != cfg.entry_dir:
            diffs.append(f"entry_dir：既有 {cfg.entry_dir}，選項 {normalize_option_path(args.entry_dir, home)}")
        if diffs:
            raise CchubError(
                f"已經有設定檔 {ctx.paths.config_file}，重新安裝會沿用它；安裝選項與它不同：\n"
                + "\n".join(f"  - {d}" for d in diffs)
                + "\n要改路徑請直接編輯 config.json，或拿掉這些選項照既有設定重裝。"
            )
    return cfg


def cmd_install(ctx: Context, args) -> int:
    _refuse_in_unit(ctx, "install")
    cfg = _install_config(ctx, args)
    plan = build_install_plan(ctx.paths, cfg, ctx.systemctl, ctx.procfs, now=ctx.wall,
                              run_doctor=lambda: cmd_doctor(ctx))
    if args.dry_run:
        print_plan(plan, "cchub install --dry-run：以下是會做的事（這次只列出，不改動任何檔案）", ctx.out)
        if plan.blockers:
            ctx.say("（真的安裝前必須先處理上面標 ⛔ 的問題）")
        return 0
    _require_own_terminal(ctx, "install")
    print_plan(plan, "cchub install：以下是會做的事", ctx.out)
    if not plan.blockers:
        if ctx.ask("確定要安裝嗎？輸入 yes 繼續：").strip().lower() != "yes":
            ctx.say("已取消，沒有改動任何東西")
            return 1
    execute_plan(plan, ctx.out)
    return 0


def cmd_uninstall(ctx: Context, args) -> int:
    _refuse_in_unit(ctx, "uninstall")
    try:
        cfg: Config | None = load_config(ctx.paths)
    except NotInstalled:
        cfg = None                                  # 沒有 config.json：入口單元名稱改從安裝紀錄取
    plan = build_uninstall_plan(ctx.paths, cfg, ctx.systemctl, ctx.procfs, now=ctx.wall)
    if args.dry_run:
        print_plan(plan, "cchub uninstall --dry-run：以下是會做的事（這次只列出，不改動任何檔案）", ctx.out)
        return 0
    _require_own_terminal(ctx, "uninstall")
    print_plan(plan, "cchub uninstall：以下是會做的事", ctx.out)
    if not plan.blockers:
        if ctx.ask("確定要移除嗎？輸入 yes 繼續：").strip().lower() != "yes":
            ctx.say("已取消，沒有改動任何東西")
            return 1
    execute_plan(plan, ctx.out)
    return 0


# ---------------------------------------------------------------- 內部指令

def cmd_serve(ctx: Context, args) -> int:
    return run_serve(ctx.paths, args.instance)


def cmd_reconcile(ctx: Context, args) -> int:
    return run_reconcile(ctx.paths, ctx.systemctl, ctx.procfs, wall=ctx.wall, out=lambda s: ctx.say(s))


# ---------------------------------------------------------------- argparse

class _ZhFormatter(argparse.RawDescriptionHelpFormatter):
    _HEADINGS = {"positional arguments": "參數", "options": "選項", "optional arguments": "選項"}

    def start_section(self, heading):
        super().start_section(self._HEADINGS.get(heading, heading))

    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, prefix if prefix is not None else "用法：")


class _Parser(argparse.ArgumentParser):
    def __init__(self, *a, **kw):
        kw.setdefault("formatter_class", _ZhFormatter)
        kw.setdefault("add_help", False)
        kw.setdefault("allow_abbrev", False)   # 不接受縮寫（例如 --brief 不能被當成 --brief-stdin）
        super().__init__(*a, **kw)
        self.add_argument("-h", "--help", action="help", help="顯示說明後結束")

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}：參數錯誤：{message}\n")


EPILOG = """第一次安裝：cchub install --projects-root <專案資料夾>（先加 --dry-run 看會做什麼）。
手機使用流程：Claude App → Code → 這台電腦的卡片 → 在入口（projects_root）開 session，
說「開新專案 X」或「打開 Y」，session 會呼叫 cchub；完成後卡片上多一筆，點它開新 session。
new／stop／restart／install／uninstall／_* 會被使用者層級的 ask 規則要求確認。"""


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="cchub", description="用手機遠端開專案與 Claude Code session：替資料夾起／顧官方 claude remote-control 伺服器。",
                epilog=EPILOG)
    p.add_argument("--version", action="version", version=f"cchub {__version__}", help="顯示版本")
    sub = p.add_subparsers(dest="cmd", metavar="<指令>", parser_class=_Parser)
    sub.required = True

    s = sub.add_parser("ls", help="列出受管伺服器：資料夾、狀態、上線時間、警告")
    s.add_argument("--json", action="store_true", help="輸出 JSON")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("open", help="替已受信任的既有資料夾起伺服器")
    s.add_argument("target", metavar="名稱或路徑", help="allowed_roots 底下的資料夾名稱，或絕對路徑")
    s.add_argument("--mode", help="權限模式（default、acceptEdits、auto、plan、dontAsk；預設看設定）")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser("logs", help="看伺服器的狀態紀錄（白名單過濾後，不含對話內容）")
    s.add_argument("target", metavar="名稱或路徑", help="資料夾名稱、絕對路徑，或 entry（入口）")
    s.add_argument("-n", type=int, default=30, metavar="N", help="行數（預設 30）")
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("doctor", help="前置條件、防護狀態、未受信任的專案清單")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("new", help="建新專案（CLAUDE.md、git、信任）並起伺服器［需確認］")
    s.add_argument("name", metavar="名稱", help="小寫英數與 -，最多 40 字；entry、hub、cchub 是保留字")
    s.add_argument("--title", help="標題（寫進 CLAUDE.md 與伺服器名稱；會清成安全字元、最多 60 字）")
    s.add_argument("--brief-stdin", action="store_true",
                   help="從 stdin 讀需求全文，寫進 CLAUDE.md 的「## 初始需求」"
                        "（搭配分隔字加單引號的 heredoc，原話不經 shell 展開）")
    s.add_argument("--mode", help="權限模式（預設看設定，通常是 auto；bypassPermissions 一律拒絕）")
    s.add_argument("--no-git", action="store_true", help="不 git init（也就不寫信任，靠上層繼承）")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("stop", help="停止伺服器［需確認］（入口不能停）")
    s.add_argument("target", metavar="名稱或路徑")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("restart", help="重啟伺服器［需確認］（在 cchub 的 session 裡重啟入口會延後 15 秒）")
    s.add_argument("target", metavar="名稱或路徑", help="資料夾名稱、絕對路徑，或 entry（入口）")
    s.set_defaults(func=cmd_restart)

    for name, fn, what in (("install", cmd_install, "安裝"), ("uninstall", cmd_uninstall, "移除")):
        s = sub.add_parser(name, help=f"{what}（只能在自己的終端機執行，會要求輸入 yes）［需確認］")
        s.add_argument("--dry-run", action="store_true", help="只列出每一步要做什麼，完全不動系統")
        if name == "install":
            s.add_argument("--projects-root", metavar="目錄",
                           help="新專案建在這裡，也是入口（還沒有 config.json 時必填；目錄必須已存在）")
            s.add_argument("--allowed-root", action="append", metavar="目錄",
                           help="open 可以打開的根目錄（可重複；預設只有 projects_root）")
            s.add_argument("--entry-dir", metavar="目錄", help="入口資料夾（預設＝projects_root）")
        s.set_defaults(func=fn)

    s = sub.add_parser("_serve", help="（systemd 內部用）替一個實例跑 supervisor")
    s.add_argument("instance", metavar="實例")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("_reconcile", help="（systemd 內部用）reconcile")
    s.set_defaults(func=cmd_reconcile)
    return p


def main(argv: list[str] | None = None, ctx: Context | None = None) -> int:
    args = build_parser().parse_args(argv)
    if ctx is None:
        ctx = Context.default()
    try:
        return int(args.func(ctx, args) or 0)
    except CchubError as e:
        print(f"❌ {e}", file=ctx.err)
        return e.exit_code
    except KeyboardInterrupt:
        print("已中斷", file=ctx.err)
        return 130
