"""reconcile：exe 被刪 → 重啟；入口不在 → 啟動（永久性錯誤時每 30 分鐘一次）；不做閒置停止。"""

import os
import time
import unittest

import helpers
from cchub.names import instance_for_dir, unit_for_instance
from cchub.reconcile import run_reconcile
from cchub.units import read_instance_config, write_instance_config, write_instance_state
from cchub.util import atomic_write_json


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.h = helpers.TempHome()
        self.sysd = helpers.FakeSystemd()
        self.entry_inst = instance_for_dir(self.h.project)
        self.entry = unit_for_instance(self.entry_inst)
        write_instance_config(self.h.paths, self.entry_inst, {"dir": self.h.project, "entry": True, "mode": "auto",
                                                              "capacity": 3})
        self.proj = self.h.mkdir("work", "projects", "proj")
        self.pinst = instance_for_dir(self.proj)
        self.punit = unit_for_instance(self.pinst)
        self.now = time.time()
        self.log = []

    def tearDown(self):
        self.h.cleanup()

    def run_rec(self, procfs=None):
        return run_reconcile(self.h.paths, self.sysd.systemctl(), procfs or helpers.FakeProcFS(),
                             wall=lambda: self.now, out=self.log.append)

    def exe_setup(self, last_change_ago, deleted=True, in_unit=True):
        self.sysd.states[self.entry] = "active"
        self.sysd.states[self.punit] = "active"
        write_instance_state(self.h.paths, self.pinst, {"child_pid": 4242, "last_change": self.now - last_change_ago})
        return helpers.FakeProcFS(exe_deleted={4242: deleted},
                                  pid_cgroups={4242: helpers.unit_cgroup(self.punit) if in_unit else "0::/other\n"})

    def test_exe_deleted_and_stable_restarts(self):
        self.assertEqual(self.run_rec(self.exe_setup(11 * 60)), 0)
        self.assertEqual([c[-1] for c in self.sysd.cmds("restart")], [self.punit])

    def test_exe_deleted_but_recent_change_waits(self):
        self.run_rec(self.exe_setup(60))
        self.assertEqual(self.sysd.cmds("restart"), [])
        self.assertTrue(any("下一輪" in m for m in self.log))

    def test_exe_fine_or_pid_elsewhere(self):
        self.run_rec(self.exe_setup(3600, deleted=False))
        self.run_rec(self.exe_setup(3600, in_unit=False))
        self.assertEqual(self.sysd.cmds("restart"), [])

    def test_entry_inactive_started(self):
        self.sysd.states[self.entry] = "inactive"
        self.run_rec()
        self.assertEqual([c[-1] for c in self.sysd.cmds("start")], [self.entry])
        self.assertEqual(self.sysd.states[self.entry], "active")

    def test_entry_active_untouched(self):
        self.sysd.states[self.entry] = "active"
        self.run_rec()
        self.assertEqual(self.sysd.cmds("start"), [])

    def test_entry_permanent_error_every_30_minutes(self):
        self.sysd.states[self.entry] = "failed"
        write_instance_state(self.h.paths, self.entry_inst, {"status": "failed",
                                                             "error": {"kind": "auth", "message": "登入失效"}})
        atomic_write_json(self.h.paths.reconcile_state_file, {"entry_last_attempt": self.now - 10 * 60})
        self.run_rec()
        self.assertEqual(self.sysd.cmds("start"), [])
        self.assertTrue(any("未滿 30 分鐘" in m for m in self.log))
        atomic_write_json(self.h.paths.reconcile_state_file, {"entry_last_attempt": self.now - 31 * 60})
        self.run_rec()
        self.assertEqual([c[-1] for c in self.sysd.cmds("start")], [self.entry])
        self.assertEqual([c[-1] for c in self.sysd.cmds("reset-failed")], [self.entry])

    def test_entry_config_rewritten_if_missing(self):
        os.unlink(self.h.paths.instance_cfg_file(self.entry_inst))
        self.sysd.states[self.entry] = "active"
        self.run_rec()
        cfg = read_instance_config(self.h.paths, self.entry_inst)
        self.assertTrue(cfg["entry"])
        self.assertEqual(cfg["dir"], self.h.project)

    def test_no_idle_stop(self):
        self.sysd.states[self.entry] = "active"
        self.sysd.states[self.punit] = "active"
        write_instance_state(self.h.paths, self.pinst, {"child_pid": 1, "last_change": self.now - 86400 * 30})
        self.run_rec(helpers.FakeProcFS(exe_deleted={1: False}, pid_cgroups={1: helpers.unit_cgroup(self.punit)}))
        self.assertEqual(self.sysd.cmds("stop"), [])


if __name__ == "__main__":
    unittest.main()
