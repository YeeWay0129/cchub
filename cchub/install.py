"""§5.7 install／uninstall。兩者都支援 --dry-run（只印出每一步，完全不動系統）。

- 偵測到自己在 cchub 單元裡（手機的 session）一律拒絕：必須在電腦的終端機執行（§5.1 規則 9）。
- ~/.claude/settings.json 的修改是「最小 diff」：只新增 permissions.ask 的 cchub 規則，其他鍵不動；
  改之前備份到 ~/.claude/backups/settings.json.backup.<YYYYMMDD>（同一天第二次起加 -2、-3…）。
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass, field
from typing import Callable

from .claudebin import select_claude
from .names import instance_for_dir, unit_for_instance
from .paths import Config, Paths, RECONCILE_SERVICE, RECONCILE_TIMER
from .reconcile import entry_instance_config
from .units import read_registry, write_instance_config, write_registry, Systemctl
from .util import CchubError, atomic_write_bytes, atomic_write_json, atomic_write_text, ensure_dir, iso_now, read_json

# §5.4：用具體子指令列舉，不用 Bash(cchub *) 萬用規則
ASK_RULES = [
    "Bash(cchub new *)",
    "Bash(cchub stop *)",
    "Bash(cchub restart *)",
    "Bash(cchub install*)",
    "Bash(cchub uninstall*)",
    "Bash(cchub _*)",
]
UNIT_TEMPLATES = ("cchub-rc@.service", RECONCILE_SERVICE, RECONCILE_TIMER)
COPY_ITEMS = ("bin", "cchub", "templates")
INSTALL_MARKER = ".cchub-install.json"


def source_root() -> str:
    """cchub 程式碼所在的根目錄（開發時是 git clone 下來的目錄，安裝後是 ~/.local/share/cchub）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def templates_dir(root: str | None = None) -> str:
    return os.path.join(root or source_root(), "templates")


# ---------------------------------------------------------------- 單元檔

def _systemd_path(p: str) -> str:
    """單元檔裡的路徑：% 要寫成 %%；含空白時加引號。"""
    p = p.replace("%", "%%")
    if any(c.isspace() for c in p) or '"' in p or "\\" in p:
        p = '"' + p.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return p


def render_template(text: str, mapping: dict[str, str]) -> str:
    for k, v in mapping.items():
        text = text.replace("@@" + k + "@@", v)
    if "@@" in text:
        raise CchubError("樣板裡還有沒代入的佔位符")
    return text


def entry_label(entry_dir: str) -> str:
    """入口在手機卡片上的名稱：git root 的名稱，不在 git 裡就是資料夾名稱。"""
    from .trust import find_git_root
    real = os.path.realpath(entry_dir)
    return os.path.basename(find_git_root(real) or real) or real


def render_skill(paths: Paths, cfg: Config, root: str | None = None) -> str:
    """SKILL.md 樣板：代入這台機器的 projects_root 與入口（@@PROJECTS_ROOT@@、@@ENTRY_DIR@@、@@ENTRY_LABEL@@）。"""
    with open(os.path.join(templates_dir(root), "SKILL.md"), "r", encoding="utf-8") as f:
        text = f.read()
    return render_template(text, {
        "PROJECTS_ROOT": _rel(paths, cfg.projects_root),
        "ENTRY_DIR": _rel(paths, cfg.entry_dir),
        "ENTRY_LABEL": entry_label(cfg.entry_dir),
    })


def rendered_units(paths: Paths, root: str | None = None) -> dict[str, str]:
    mapping = {
        "CCHUB_BIN": _systemd_path(paths.install_bin),
        "UNIT_PATH": paths.unit_path_env().replace("%", "%%"),
    }
    out = {}
    for name in UNIT_TEMPLATES:
        with open(os.path.join(templates_dir(root), name), "r", encoding="utf-8") as f:
            out[name] = render_template(f.read(), mapping)
    return out


def _summary_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


# ---------------------------------------------------------------- settings.json（最小 diff）

@dataclass(frozen=True)
class JsonFormat:
    indent: object          # 2、4 或 "\t"
    newline: bool

    def dump(self, data) -> str:
        return json.dumps(data, indent=self.indent, ensure_ascii=False) + ("\n" if self.newline else "")


def detect_format(text: str, data) -> JsonFormat | None:
    """找出能把原檔「一字不差」重現的格式；找不到就回傳 None（這時不自動改，避免大 diff）。"""
    for indent in (2, 4, "\t"):
        for nl in (True, False):
            fmt = JsonFormat(indent, nl)
            if fmt.dump(data) == text:
                return fmt
    return None


def add_ask_rules(settings: dict, rules: list[str]) -> tuple[dict, list[str], bool, bool]:
    """回傳 (新設定, 實際新增的規則, 是否新建 ask 鍵, 是否新建 permissions 鍵)。

    為了最小 diff：新建的鍵放在最前面、新規則插在清單最前面（純插入，不改到既有行）。
    """
    new = copy.deepcopy(settings)
    created_perm = created_ask = False
    if "permissions" not in new:
        perm: dict = {}
        new = {"permissions": perm, **new}
        created_perm = True
    else:
        perm = new["permissions"]
    if not isinstance(perm, dict):
        raise CchubError("settings.json 的 permissions 不是物件，無法自動加規則")
    if "ask" not in perm:
        ask: list = []
        rebuilt = {"ask": ask, **perm}
        perm.clear()
        perm.update(rebuilt)
        created_ask = True
    else:
        ask = perm["ask"]
    if not isinstance(ask, list):
        raise CchubError("settings.json 的 permissions.ask 不是陣列，無法自動加規則")
    missing = [r for r in rules if r not in ask]
    ask[0:0] = missing
    return new, missing, created_ask, created_perm


def remove_ask_rules(settings: dict, rules: list[str], drop_empty_ask: bool, drop_empty_perm: bool) -> tuple[dict, list[str]]:
    new = copy.deepcopy(settings)
    perm = new.get("permissions")
    if not isinstance(perm, dict) or not isinstance(perm.get("ask"), list):
        return new, []
    removed = [r for r in perm["ask"] if r in rules]
    perm["ask"] = [r for r in perm["ask"] if r not in rules]
    if drop_empty_ask and not perm["ask"]:
        del perm["ask"]
    if drop_empty_perm and not perm:
        del new["permissions"]
    return new, removed


def _top_block(key: str, value, fmt: JsonFormat) -> str:
    unit = fmt.indent if isinstance(fmt.indent, str) else " " * fmt.indent
    body = json.dumps(value, indent=fmt.indent, ensure_ascii=False).replace("\n", "\n" + unit)
    return unit + json.dumps(key, ensure_ascii=False) + ": " + body


def permissions_diff(before_text: str, before: dict, after_text: str, after: dict, fmt: JsonFormat,
                     label: str) -> list[str]:
    """只列 permissions 區塊的 unified diff（行號是檔案裡的真實行號）。"""
    def block(text: str, data: dict) -> tuple[list[str], int]:
        if "permissions" not in data:
            return [], 0
        b = _top_block("permissions", data["permissions"], fmt)
        pos = text.find(b)
        start = text[:pos].count("\n") if pos >= 0 else 0
        return b.split("\n"), start

    a_lines, a_off = block(before_text, before)
    b_lines, b_off = block(after_text, after)
    out = []
    for line in difflib.unified_diff(a_lines, b_lines, fromfile=f"{label}（修改前）",
                                     tofile=f"{label}（修改後）", lineterm="", n=3):
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@", line)
            if m:
                line = (f"@@ -{int(m.group(1)) + a_off}{m.group(2) or ''} "
                        f"+{int(m.group(3)) + b_off}{m.group(4) or ''} @@")
        out.append(line)
    return out


def settings_backup_path(backups_dir: str, now: float) -> str:
    """~/.claude/backups/settings.json.backup.<YYYYMMDD>；同一天第二次起加 -2、-3…"""
    base = os.path.join(backups_dir, "settings.json.backup." + time.strftime("%Y%m%d", time.localtime(now)))
    if not os.path.lexists(base):
        return base
    n = 2
    while os.path.lexists(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


@dataclass
class SettingsChange:
    path: str
    exists: bool
    before_text: str
    before: dict
    after: dict
    fmt: JsonFormat
    changed_rules: list[str]
    created_ask: bool = False
    created_perm: bool = False

    @property
    def after_text(self) -> str:
        return self.fmt.dump(self.after)

    def diff(self, label: str) -> list[str]:
        return permissions_diff(self.before_text, self.before, self.after_text, self.after, self.fmt, label)


def _read_settings(path: str) -> tuple[bool, str, dict, JsonFormat]:
    target = os.path.realpath(path)
    if not os.path.exists(target):
        return False, "", {}, JsonFormat(2, True)
    with open(target, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        data = json.loads(text)
    except ValueError as e:
        raise CchubError(f"{path} 解析失敗，無法自動加規則：{e}") from e
    if not isinstance(data, dict):
        raise CchubError(f"{path} 不是 JSON 物件")
    fmt = detect_format(text, data)
    if fmt is None:
        raise CchubError(
            f"{path} 的排版無法一字不差地重現，為了避免大 diff 不自動修改；"
            f"請手動在 permissions.ask 加入：{', '.join(ASK_RULES)}"
        )
    return True, text, data, fmt


def plan_settings_add(path: str) -> SettingsChange:
    exists, text, data, fmt = _read_settings(path)
    after, added, created_ask, created_perm = add_ask_rules(data, ASK_RULES)
    return SettingsChange(path, exists, text, data, after, fmt, added, created_ask, created_perm)


def plan_settings_remove(path: str, rules: list[str], drop_empty_ask: bool, drop_empty_perm: bool) -> SettingsChange:
    exists, text, data, fmt = _read_settings(path)
    after, removed = remove_ask_rules(data, rules, drop_empty_ask, drop_empty_perm)
    return SettingsChange(path, exists, text, data, after, fmt, removed)


def _only_ask_differs(a: dict, b: dict) -> bool:
    def strip(d):
        d = copy.deepcopy(d)
        p = d.get("permissions")
        if isinstance(p, dict):
            p.pop("ask", None)
            if not p:
                d.pop("permissions", None)
        return d
    return strip(a) == strip(b)


def apply_settings_change(ch: SettingsChange, backups_dir: str, now: float) -> str | None:
    """先備份再寫；寫完回讀確認只有 permissions.ask 變了。回傳備份路徑。"""
    if not ch.changed_rules:
        return None
    target = os.path.realpath(ch.path)
    backup = None
    mode = 0o644
    if ch.exists:
        ensure_dir(backups_dir, 0o700)
        backup = settings_backup_path(backups_dir, now)
        shutil.copy2(target, backup)
        mode = stat.S_IMODE(os.stat(target).st_mode)
    else:
        ensure_dir(os.path.dirname(target), 0o700)
    atomic_write_text(target, ch.after_text, mode=mode)
    with open(target, "r", encoding="utf-8") as f:
        back = json.loads(f.read())
    if back != ch.after or not _only_ask_differs(back, ch.before):
        raise CchubError(f"{ch.path} 回讀不符，請用備份 {backup} 還原")
    return backup


# ---------------------------------------------------------------- 計畫

@dataclass
class Step:
    title: str
    details: list[str] = field(default_factory=list)
    run: Callable[[], None] | None = None


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


def _rel(paths: Paths, p: str) -> str:
    return "~" + p[len(paths.home):] if p.startswith(paths.home + "/") else p


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return _sha256_bytes(f.read())
    except OSError:
        return None


def _file_state(path: str, text: str) -> str:
    """absent／same／different（目標已存在而內容不同）。"""
    if not os.path.lexists(path):
        return "absent"
    return "same" if _sha256_file(path) == _sha256_bytes(text.encode("utf-8")) else "different"


def _unit_backup_path(dest: str) -> str:
    """單元檔的備份：同目錄 <檔名>.bak（已存在就 .bak-2、.bak-3…）。副檔名不是單元型別，systemd 不會載入。"""
    base = dest + ".bak"
    if not os.path.lexists(base):
        return base
    n = 2
    while os.path.lexists(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


def _dated_backup_path(backups_dir: str, stem: str, now: float) -> str:
    """~/.claude/backups/<stem>.backup.<YYYYMMDD>；同一天第二次起加 -2、-3…"""
    base = os.path.join(backups_dir, f"{stem}.backup." + time.strftime("%Y%m%d", time.localtime(now)))
    if not os.path.lexists(base):
        return base
    n = 2
    while os.path.lexists(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


SKILL_BACKUP_STEM = "skills-cchub-SKILL.md"


def _record_installed_file(paths: Paths, path: str, content: bytes, backup: str | None) -> None:
    reg = read_registry(paths)
    files = reg.setdefault("install", {}).setdefault("files", {})
    prev = files.get(path) if isinstance(files.get(path), dict) else {}
    files[path] = {"sha256": _sha256_bytes(content), "backup": backup or prev.get("backup")}
    write_registry(paths, reg)


def _write_managed_file(paths: Paths, dest: str, text: str, backup_path: Callable[[], str]) -> str | None:
    """寫 cchub 管理的檔案：已存在且內容不同 → 先備份（D8）；registry 記下寫入內容的 sha256。回傳備份路徑。"""
    backup = None
    if _file_state(dest, text) == "different":
        backup = backup_path()
        ensure_dir(os.path.dirname(backup), 0o700)
        shutil.copy2(dest, backup, follow_symlinks=False)
    data = text.encode("utf-8")
    atomic_write_bytes(dest, data, mode=0o644)
    _record_installed_file(paths, dest, data, backup)
    return backup


def _removal_decision(files_rec: dict, path: str) -> str:
    """uninstall：只刪 sha256 與安裝紀錄相符的檔案（D8）。"""
    if not os.path.lexists(path):
        return "absent"
    rec = files_rec.get(path)
    if not isinstance(rec, dict) or not rec.get("sha256"):
        return "keep-unrecorded"
    return "delete" if _sha256_file(path) == rec["sha256"] else "keep-modified"


def _copy_tree(src_root: str, dest: str) -> None:
    """複製 bin/、cchub/、templates/ 到 dest（先寫到暫存資料夾再換上去）。"""
    parent = os.path.dirname(dest)
    ensure_dir(parent, 0o755)
    tmp = f"{dest}.new-{os.getpid()}"
    old = f"{dest}.old-{os.getpid()}"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp, 0o755)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for item in COPY_ITEMS:
        shutil.copytree(os.path.join(src_root, item), os.path.join(tmp, item), ignore=ignore)
    with open(os.path.join(tmp, INSTALL_MARKER), "w", encoding="utf-8") as f:
        json.dump({"installed_at": iso_now(), "source": src_root}, f, ensure_ascii=False, indent=2)
    if os.path.exists(dest):
        if not os.path.exists(os.path.join(dest, INSTALL_MARKER)):
            shutil.rmtree(tmp)
            raise CchubError(f"{dest} 已存在但不是 cchub 安裝的，不覆蓋")
        os.rename(dest, old)
    os.rename(tmp, dest)
    if os.path.exists(old):
        shutil.rmtree(old)


def build_install_plan(paths: Paths, cfg: Config, systemctl: Systemctl, procfs, *,
                       src_root: str | None = None, now: Callable[[], float] = time.time,
                       run_doctor: Callable[[], None] | None = None) -> Plan:
    src = src_root or source_root()
    plan = Plan()
    if procfs.in_cchub_unit():
        plan.blockers.append("偵測到在 cchub 管理的單元裡執行（手機的 session）：install 只能在電腦的終端機執行")
    if os.path.realpath(src) == os.path.realpath(paths.install_dir):
        plan.blockers.append(f"請從開發目錄（git clone 下來的目錄）執行 bin/cchub install，不要從 {paths.install_dir}")
    for item in COPY_ITEMS:
        if not os.path.isdir(os.path.join(src, item)):
            plan.blockers.append(f"來源缺少 {item}/：{src}")

    entry = os.path.realpath(cfg.entry_dir)
    entry_inst = instance_for_dir(entry)
    entry_unit = unit_for_instance(entry_inst)

    # 手動開的 rc（§5.4 上線前）
    manual = [p for p in procfs.rc_under(entry) if not p.cchub_unit]
    for p in manual:
        where = "入口資料夾" if p.cwd == entry else "入口底下的資料夾"
        msg = f"{where} {p.cwd} 已有手動開的 Remote Control 程序（pid {p.pid}）：請先到那個終端機按 Ctrl+C 關掉"
        (plan.blockers if p.cwd == entry else plan.warnings).append(msg)

    exe, warns = select_claude(paths.native_cli_root, paths.desktop_cli_root)
    plan.warnings.extend(warns)

    # 1. 複製
    plan.steps.append(Step(
        f"複製程式到 {_rel(paths, paths.install_dir)}（放在 projects_root 之外，入口的 session 改寫不到）",
        [f"{src}/{i}/ → {_rel(paths, paths.install_dir)}/{i}/（排除 __pycache__）" for i in COPY_ITEMS]
        + [f"寫入安裝標記 {_rel(paths, paths.install_dir)}/{INSTALL_MARKER}"],
        lambda: _copy_tree(src, paths.install_dir),
    ))

    # 2. symlink
    link, target = paths.bin_link, paths.install_bin
    if os.path.lexists(link):
        if not (os.path.islink(link) and os.readlink(link) == target):
            plan.blockers.append(f"{link} 已存在且不是指向 {target} 的連結，不覆蓋")

    def do_link() -> None:
        ensure_dir(os.path.dirname(link), 0o755)
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(target, link)

    plan.steps.append(Step(f"建立 {_rel(paths, link)} → {_rel(paths, target)}（讓 session 用名稱呼叫、ask 規則比對得到）",
                           [f"ln -s {target} {link}"], do_link))

    # 3. 單元檔
    try:
        units = rendered_units(paths, src)
    except (OSError, CchubError) as e:
        units = {}
        plan.blockers.append(f"讀不到單元樣板：{e}")
    details = []
    for name, text in units.items():
        dest = os.path.join(paths.systemd_user_dir, name)
        state = _file_state(dest, text)
        if state == "different":
            plan.warnings.append(f"{_rel(paths, dest)} 已存在而且內容不同：安裝時會先備份到 "
                                 f"{_rel(paths, _unit_backup_path(dest))} 再覆蓋")
        details.append(f"{_rel(paths, dest)}：" + {"absent": "（新建）", "same": "（已存在、內容相同）",
                                                  "different": "（⚠️ 已存在且內容不同 → 先備份再覆蓋）"}[state])
        details.extend("    " + ln for ln in _summary_lines(text))
    details.append("systemctl --user daemon-reload")

    def do_units() -> None:
        ensure_dir(paths.systemd_user_dir, 0o755)
        for name, text in units.items():
            dest = os.path.join(paths.systemd_user_dir, name)
            _write_managed_file(paths, dest, text, lambda d=dest: _unit_backup_path(d))
        systemctl.daemon_reload()

    plan.steps.append(Step("寫入 systemd 單元與計時器", details, do_units))

    # 4. skill
    skill_src = os.path.join(templates_dir(src), "SKILL.md")
    try:
        skill_text = render_skill(paths, cfg, src)
    except (OSError, CchubError) as e:
        skill_text = ""
        plan.blockers.append(f"讀不到 skill 樣板：{e}")
    skill_details = [f"來源：{skill_src}",
                     f"代入：projects_root＝{_rel(paths, cfg.projects_root)}、入口＝{_rel(paths, cfg.entry_dir)}"
                     f"（卡片上的名稱「{entry_label(cfg.entry_dir)}」）"]
    if _file_state(paths.skill_file, skill_text) == "different":
        bk = _dated_backup_path(paths.backups_dir, SKILL_BACKUP_STEM, now())
        plan.warnings.append(f"{_rel(paths, paths.skill_file)} 已存在而且內容不同：安裝時會先備份到 {_rel(paths, bk)} 再覆蓋")
        skill_details.append(f"⚠️ 已存在且內容不同 → 先備份到 {_rel(paths, bk)}")

    def do_skill() -> None:
        ensure_dir(paths.skill_dir, 0o755)
        _write_managed_file(paths, paths.skill_file, skill_text,
                            lambda: _dated_backup_path(paths.backups_dir, SKILL_BACKUP_STEM, now()))

    plan.steps.append(Step(f"寫入 skill {_rel(paths, paths.skill_file)}", skill_details, do_skill))

    # 5. settings.json
    ch: SettingsChange | None = None
    try:
        ch = plan_settings_add(paths.claude_settings)
    except CchubError as e:
        plan.blockers.append(str(e))
    sdetails = []
    if ch is not None:
        if ch.changed_rules:
            sdetails.append(f"先備份到 {_rel(paths, settings_backup_path(paths.backups_dir, now()))}"
                            "（同一天已有備份則用 -2、-3…）" if ch.exists else "檔案不存在，會新建")
            sdetails.append(f"只新增 permissions.ask 的 {len(ch.changed_rules)} 條規則，其他鍵完全不動：")
            sdetails.extend(f"  + {r}" for r in ch.changed_rules)
            sdetails.append("差異（只列 permissions 區塊）：")
            sdetails.extend("  " + ln for ln in ch.diff(_rel(paths, paths.claude_settings)))
        else:
            sdetails.append("六條 ask 規則都已存在，不修改")

    def do_settings() -> None:
        cur = plan_settings_add(paths.claude_settings)   # 執行當下重讀，避免覆蓋規劃後的變更
        backup = apply_settings_change(cur, paths.backups_dir, now())
        ch_ = cur
        reg = read_registry(paths)
        rec = reg.setdefault("install", {})
        prev = rec.get("settings") or {}
        rec["settings"] = {
            "file": paths.claude_settings,
            "backup": backup or prev.get("backup"),
            "rules_added": [r for r in ASK_RULES if r in set(prev.get("rules_added") or []) | set(ch_.changed_rules)],
            "created_ask": bool(prev.get("created_ask")) or ch_.created_ask,
            "created_permissions": bool(prev.get("created_permissions")) or ch_.created_perm,
        }
        write_registry(paths, reg)

    plan.steps.append(Step(f"在 {_rel(paths, paths.claude_settings)} 加使用者層級 ask 規則（F23；體驗層，不是安全邊界）",
                           sdetails, do_settings))

    # 6. 設定、入口實例、enable
    cfg_json = {k: v for k, v in cfg.to_json().items()}
    cfg_detail = [f"{_rel(paths, paths.config_file)}："
                  + ("已存在，不動" if os.path.exists(paths.config_file) else "不存在，寫入預設值")]
    if not os.path.exists(paths.config_file):
        cfg_detail.extend("    " + ln for ln in json.dumps(cfg_json, ensure_ascii=False, indent=2).splitlines())
    ecfg = entry_instance_config(cfg, now())
    cfg_detail.append(f"{_rel(paths, paths.instance_cfg_file(entry_inst))}：入口（{entry}，模式 {ecfg['mode']}，capacity {ecfg['capacity']}）")
    cfg_detail.append(f"systemctl --user enable --now {entry_unit} {RECONCILE_TIMER}")
    cfg_detail.append("（只有入口與計時器 enable；專案伺服器不 enable）")
    if manual:
        cfg_detail.append("手動開的 Remote Control：" + "、".join(f"pid {p.pid}（{p.cwd}）" for p in manual)
                          + " → 安裝前請先 Ctrl+C")
    else:
        cfg_detail.append(f"檢查過：{entry} 底下沒有手動開的 Remote Control 程序")
    cfg_detail.append("若入口已經在跑（重新安裝），要套用新版程式請之後執行 cchub restart entry")

    def do_enable() -> None:
        if not os.path.exists(paths.config_file):
            ensure_dir(paths.config_dir)
            atomic_write_json(paths.config_file, cfg_json)
        write_instance_config(paths, entry_inst, ecfg)
        systemctl.enable([entry_unit, RECONCILE_TIMER], now=True)
        reg = read_registry(paths)
        rec = reg.setdefault("install", {})
        rec.update({"installed_at": iso_now(now()), "source": src, "entry_unit": entry_unit,
                    "units": list(UNIT_TEMPLATES), "skill": paths.skill_file, "symlink": link})
        write_registry(paths, reg)

    plan.steps.append(Step(f"替入口寫實例設定並 enable --now（{entry_unit}）", cfg_detail, do_enable))

    # 7. doctor
    plan.steps.append(Step("執行 cchub doctor", ["檢查前置條件、防護狀態、未受信任的專案"], run_doctor))
    return plan


def build_uninstall_plan(paths: Paths, cfg: Config | None, systemctl: Systemctl, procfs, *,
                         now: Callable[[], float] = time.time) -> Plan:
    plan = Plan()
    if procfs.in_cchub_unit():
        plan.blockers.append("偵測到在 cchub 管理的單元裡執行（手機的 session）：uninstall 只能在電腦的終端機執行")
    reg = read_registry(paths)
    if cfg is not None:
        entry_unit: str | None = unit_for_instance(instance_for_dir(os.path.realpath(cfg.entry_dir)))
    else:                                           # 沒有 config.json：改用安裝紀錄
        entry_unit = (reg.get("install") or {}).get("entry_unit") or None
        if not entry_unit:
            plan.warnings.append("沒有 config.json 也沒有安裝紀錄，找不到入口單元名稱：只停掉正在跑的 cchub-rc@ 單元與計時器")
    running = systemctl.list_rc_units()

    to_disable = [u for u in (entry_unit, RECONCILE_TIMER) if u]

    def do_stop() -> None:
        systemctl.disable(to_disable, now=True)
        for u in systemctl.list_rc_units():
            systemctl.stop(u)

    plan.steps.append(Step("停用並停止所有 cchub 單元與計時器",
                           [f"systemctl --user disable --now {' '.join(to_disable)}"]
                           + [f"systemctl --user stop {u}" for u in running], do_stop))

    files_rec = (reg.get("install") or {}).get("files") or {}
    if not isinstance(files_rec, dict):
        files_rec = {}
    unit_files = [os.path.join(paths.systemd_user_dir, n) for n in UNIT_TEMPLATES]

    def describe(path: str) -> str:
        d = _removal_decision(files_rec, path)
        rec = files_rec.get(path) if isinstance(files_rec.get(path), dict) else {}
        note = f"；安裝前的原檔備份在 {_rel(paths, rec['backup'])}，需要可自行還原" if rec.get("backup") else ""
        if d == "keep-modified":
            plan.warnings.append(f"{_rel(paths, path)} 在安裝後被改過（sha256 不符），保留不刪")
        elif d == "keep-unrecorded":
            plan.warnings.append(f"{_rel(paths, path)} 沒有安裝紀錄（不確定是不是 cchub 寫的），保留不刪")
        return {"delete": f"rm {_rel(paths, path)}（sha256 與安裝時相符）{note}",
                "keep-modified": f"保留 {_rel(paths, path)}（內容已被改過）{note}",
                "keep-unrecorded": f"保留 {_rel(paths, path)}（沒有安裝紀錄）",
                "absent": f"（{_rel(paths, path)} 不存在，略過）"}[d]

    def remove_if_ours(path: str) -> None:
        if _removal_decision(files_rec, path) == "delete":
            os.unlink(path)

    def do_units() -> None:
        for f in unit_files:
            remove_if_ours(f)
        systemctl.daemon_reload()

    plan.steps.append(Step("移除單元檔並 daemon-reload（只刪 sha256 與安裝紀錄相符的）",
                           [describe(f) for f in unit_files], do_units))

    def do_skill() -> None:
        remove_if_ours(paths.skill_file)
        try:
            os.rmdir(paths.skill_dir)
        except OSError:
            pass

    plan.steps.append(Step("移除 skill（只刪 sha256 與安裝紀錄相符的）", [describe(paths.skill_file)], do_skill))

    srec = (reg.get("install") or {}).get("settings")
    recorded = isinstance(srec, dict) and isinstance(srec.get("rules_added"), list)
    rules = [r for r in (srec.get("rules_added") if recorded else []) if r in ASK_RULES]
    created_ask = bool(srec.get("created_ask")) if recorded else False
    created_perm = bool(srec.get("created_permissions")) if recorded else False
    ch: SettingsChange | None = None
    sdetails = []
    if not recorded:
        # D7：沒有紀錄就什麼都不刪，避免刪到使用者自己的規則
        sdetails.append("registry 沒有「cchub 加了哪些規則」的紀錄，這一步不動 settings.json。")
        sdetails.append("如需移除，請手動檢查 ~/.claude/settings.json 的 permissions.ask，只刪確定是 cchub 加的：")
        sdetails.extend(f"  {r}" for r in ASK_RULES)
    elif not rules:
        sdetails.append("安裝時這些規則原本就在，cchub 沒有新增任何規則 → 不修改")
    else:
        try:
            ch = plan_settings_remove(paths.claude_settings, rules, created_ask, created_perm)
        except CchubError as e:
            plan.blockers.append(str(e))
        if ch is not None and ch.changed_rules:
            sdetails.append(f"先備份到 {_rel(paths, settings_backup_path(paths.backups_dir, now()))}")
            sdetails.append("只移除 registry 記錄為 cchub 新增的 ask 規則：")
            sdetails.extend(f"  - {r}" for r in ch.changed_rules)
            if created_ask:
                sdetails.append("「ask」鍵是 cchub 建立的：清空後一併移除")
            sdetails.append("差異（只列 permissions 區塊）：")
            sdetails.extend("  " + ln for ln in ch.diff(_rel(paths, paths.claude_settings)))
        elif ch is not None:
            sdetails.append("那些規則已經不在了，不修改")

    def do_settings() -> None:
        if not recorded or not rules:
            return
        cur = plan_settings_remove(paths.claude_settings, rules, created_ask, created_perm)
        apply_settings_change(cur, paths.backups_dir, now())

    plan.steps.append(Step(f"從 {_rel(paths, paths.claude_settings)} 移除 cchub 的 ask 規則", sdetails, do_settings))

    keys = list(reg.get("trust_keys") or [])

    def do_trust() -> None:
        from .trust import revoke_trust_keys
        revoke_trust_keys(paths, keys)
        r = read_registry(paths)
        r["trust_keys"] = []
        write_registry(paths, r)

    plan.steps.append(Step("依 registry 撤回 cchub 加過的信任鍵（只把 hasTrustDialogAccepted 改回 false）",
                           [f"  {k}" for k in keys] or ["（沒有）"], do_trust))

    def do_files() -> None:
        if os.path.islink(paths.bin_link) and os.readlink(paths.bin_link) == paths.install_bin:
            os.unlink(paths.bin_link)
        if os.path.exists(os.path.join(paths.install_dir, INSTALL_MARKER)):
            shutil.rmtree(paths.install_dir)

    plan.steps.append(Step(f"移除 {_rel(paths, paths.bin_link)} 與 {_rel(paths, paths.install_dir)}",
                           ["只移除 cchub 自己的連結與安裝目錄（有安裝標記才刪）",
                            f"保留 {_rel(paths, paths.config_dir)} 與 {_rel(paths, paths.state_dir)}；建出來的專案資料夾不刪"],
                           do_files))
    return plan


def print_plan(plan: Plan, title: str, out) -> None:
    print(title, file=out)
    for w in plan.warnings:
        print(f"⚠️ {w}", file=out)
    for b in plan.blockers:
        print(f"⛔ {b}", file=out)
    for i, s in enumerate(plan.steps, 1):
        print(f"[{i}/{len(plan.steps)}] {s.title}", file=out)
        for d in s.details:
            print(f"    {d}", file=out)


def execute_plan(plan: Plan, out) -> None:
    if plan.blockers:
        raise CchubError("有必須先處理的問題，未做任何變更：\n" + "\n".join(f"  - {b}" for b in plan.blockers))
    for i, s in enumerate(plan.steps, 1):
        print(f"[{i}/{len(plan.steps)}] {s.title} …", file=out)
        if s.run is not None:
            s.run()
    print("完成。", file=out)
