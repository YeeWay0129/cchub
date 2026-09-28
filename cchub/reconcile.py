"""§5.3 `cchub _reconcile`（cchub-reconcile.timer 每 5 分鐘觸發）。

- 伺服器的 /proc/<pid>/exe 已被刪除（F14：更新刪掉舊版），且 10 分鐘內狀態沒有變化 → 重啟（_serve 會挑新版）。
- 入口沒在跑：一般情況 → 啟動；狀態檔有永久性錯誤 → 每 30 分鐘才重試一次（重新登入後會自己恢復）。
- 不自動停閒置伺服器（使用者決定，§12 Q4）。
"""

from __future__ import annotations

import os
import time
from typing import Callable

from .names import instance_for_dir, instance_from_unit, unit_for_instance
from .paths import Paths, load_config
from .procfs import ProcFS
from .serve import PERMANENT_KINDS
from .units import (RUNNING_STATES, Systemctl, read_instance_config, read_instance_state,
                    write_instance_config)
from .util import CchubError, atomic_write_json, ensure_dir, iso_now, read_json

EXE_STABLE_SECONDS = 600         # 10 分鐘內狀態沒有變化才重啟
PERMANENT_RETRY_SECONDS = 1800   # 入口有永久性錯誤時，每 30 分鐘才重試一次


def entry_instance_config(cfg, now: float) -> dict:
    return {
        "dir": os.path.realpath(cfg.entry_dir),
        "title": os.path.basename(os.path.realpath(cfg.entry_dir)),
        "mode": cfg.entry_mode,
        "capacity": cfg.default_capacity,
        "entry": True,
        "created_by": "install",
        "created_at": iso_now(now),
    }


def run_reconcile(paths: Paths, systemctl: Systemctl, procfs: ProcFS, *,
                  wall: Callable[[], float] = time.time,
                  out: Callable[[str], None] = print) -> int:
    cfg = load_config(paths)
    entry_inst = instance_for_dir(os.path.realpath(cfg.entry_dir))
    entry_unit = unit_for_instance(entry_inst)
    rc = 0

    # 1) 執行檔被更新刪掉的伺服器 → 重啟
    for unit in systemctl.list_rc_units():
        inst = instance_from_unit(unit)
        if inst is None:
            continue
        st = read_instance_state(paths, inst)
        pid = st.get("child_pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        if not procfs.pid_in_unit(pid, unit):
            continue                  # 狀態檔的 pid 已不屬於這個單元（過期或被重用）
        if procfs.exe_deleted(pid) is not True:
            continue
        age = wall() - float(st.get("last_change") or 0)
        if age < EXE_STABLE_SECONDS:
            out(f"[reconcile] {unit}：執行檔已被刪除，但狀態 {int(age)} 秒前才變過，下一輪再看")
            continue
        out(f"[reconcile] {unit}：執行檔已被更新刪除（F14），重啟以換成新版")
        try:
            systemctl.restart(unit)
        except CchubError as e:
            out(f"[reconcile] 重啟失敗：{e}")
            rc = 1

    # 2) 確保入口在跑
    try:
        if read_instance_config(paths, entry_inst) is None:
            write_instance_config(paths, entry_inst, entry_instance_config(cfg, wall()))
            out(f"[reconcile] 補寫入口的實例設定：{paths.instance_cfg_file(entry_inst)}")
        state = systemctl.active_state(entry_unit)
        if state in RUNNING_STATES:
            return rc
        est = read_instance_state(paths, entry_inst)
        err = est.get("error") if isinstance(est.get("error"), dict) else {}
        rec = read_json(paths.reconcile_state_file, default={}) or {}
        if not isinstance(rec, dict):
            rec = {}
        last = float(rec.get("entry_last_attempt") or 0)
        if est.get("status") == "failed" and err.get("kind") in PERMANENT_KINDS:
            if wall() - last < PERMANENT_RETRY_SECONDS:
                out(f"[reconcile] 入口有永久性錯誤（{err.get('kind')}：{err.get('message')}），"
                    f"距上次重試 {int(wall() - last)} 秒，未滿 30 分鐘，這輪不動")
                return rc
            out(f"[reconcile] 入口有永久性錯誤（{err.get('kind')}），已滿 30 分鐘，重試一次")
        else:
            out(f"[reconcile] 入口沒在跑（{state}），啟動")
        rec["entry_last_attempt"] = wall()
        ensure_dir(paths.state_dir)
        atomic_write_json(paths.reconcile_state_file, rec)
        systemctl.reset_failed(entry_unit)
        systemctl.start(entry_unit)
    except CchubError as e:
        out(f"[reconcile] 處理入口時出錯：{e}")
        rc = 1
    return rc
