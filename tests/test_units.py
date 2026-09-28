"""units：上限計數（AC13 第 7 個被拒）、flock 互斥、systemctl 包裝、實例檔；procfs 對假的 /proc 樹。"""

import os
import threading
import time
import unittest

import helpers
from cchub.names import instance_for_dir, unit_for_instance
from cchub.paths import load_config
from cchub.procfs import ProcFS, is_rc_argv
from cchub.units import (check_capacity, delayed_action_argv, list_instance_configs, read_instance_config,
                         state_lock, write_instance_config)
from cchub.util import CchubError


class CapacityTest(unittest.TestCase):
    def setUp(self):
        self.h = helpers.TempHome()
        self.cfg = load_config(self.h.paths)
        self.sysd = helpers.FakeSystemd()
        self.entry = unit_for_instance(instance_for_dir(self.h.project))
        self.sysd.states[self.entry] = "active"

    def tearDown(self):
        self.h.cleanup()

    def unit(self, i):
        return unit_for_instance(instance_for_dir(f"{self.h.project}/p{i}"))

    def test_seventh_rejected(self):
        for i in range(6):
            self.sysd.states[self.unit(i)] = "active" if i % 2 else "activating"
        with self.assertRaises(CchubError) as cm:
            check_capacity(self.sysd.systemctl(), self.cfg, self.entry, self.unit(7))
        msg = str(cm.exception)
        self.assertIn("上限 6", msg)
        self.assertIn(f"{self.h.project}/p0", msg)         # 列出目前在跑的

    def test_sixth_allowed_entry_not_counted(self):
        for i in range(5):
            self.sysd.states[self.unit(i)] = "active"
        check_capacity(self.sysd.systemctl(), self.cfg, self.entry, self.unit(6))

    def test_target_itself_not_counted_and_failed_not_counted(self):
        for i in range(6):
            self.sysd.states[self.unit(i)] = "active"
        check_capacity(self.sysd.systemctl(), self.cfg, self.entry, self.unit(0))   # 重啟已在跑的
        self.sysd.states[self.unit(5)] = "failed"
        check_capacity(self.sysd.systemctl(), self.cfg, self.entry, self.unit(9))

    def test_list_units_parsing(self):
        sysd = helpers.FakeSystemd()
        sysd.states.update({"cchub-rc@a\\x20b.service": "active", "cchub-rc@c.service": "inactive",
                            "other.service": "active"})
        self.assertEqual(sysd.systemctl().list_rc_units(), ["cchub-rc@a\\x20b.service"])
        cmd = sysd.calls[-1]
        self.assertEqual(cmd[:3], ["systemctl", "--user", "list-units"])
        self.assertIn("--state=active,activating,reloading", cmd)


class FlockTest(unittest.TestCase):
    def setUp(self):
        self.h = helpers.TempHome()

    def tearDown(self):
        self.h.cleanup()

    def test_mutual_exclusion(self):
        events = []
        inside = threading.Event()

        def holder():
            with state_lock(self.h.paths):
                events.append("A-in")
                inside.set()
                time.sleep(0.5)
                events.append("A-out")

        t = threading.Thread(target=holder)
        t.start()
        inside.wait(5)
        t0 = time.monotonic()
        with state_lock(self.h.paths):
            waited = time.monotonic() - t0
            events.append("B-in")
        t.join()
        self.assertEqual(events, ["A-in", "A-out", "B-in"])
        self.assertGreater(waited, 0.3)

    def test_timeout(self):
        inside, release = threading.Event(), threading.Event()

        def holder():
            with state_lock(self.h.paths):
                inside.set()
                release.wait(5)

        t = threading.Thread(target=holder)
        t.start()
        inside.wait(5)
        try:
            with self.assertRaises(CchubError) as cm:
                with state_lock(self.h.paths, timeout=0.3):
                    pass
            self.assertIn("另一個 cchub 操作", str(cm.exception))
        finally:
            release.set()
            t.join()


class MiscTest(unittest.TestCase):
    def test_delayed_argv(self):
        self.assertEqual(delayed_action_argv("restart", "cchub-rc@home-alice-work-projects.service"),
                         ["systemd-run", "--user", "--on-active=15s", "systemctl", "--user", "restart",
                          "cchub-rc@home-alice-work-projects.service"])

    def test_instance_config_roundtrip_escaped_name(self):
        h = helpers.TempHome()
        try:
            d = h.mkdir("work", "projects", "local llm", "中文")
            inst = instance_for_dir(d)
            self.assertIn("\\x20", inst)
            write_instance_config(h.paths, inst, {"dir": d, "mode": "auto"})
            self.assertEqual(read_instance_config(h.paths, inst)["dir"], d)
            self.assertEqual(list(list_instance_configs(h.paths)), [inst])
        finally:
            h.cleanup()


class ProcFSTest(unittest.TestCase):
    """用假的 /proc 樹測真的 ProcFS。"""

    def setUp(self):
        self.h = helpers.TempHome()
        self.root = os.path.join(self.h.tmp, "proc")
        self.proj = self.h.mkdir("work", "projects", "proj")

    def tearDown(self):
        self.h.cleanup()

    def mkproc(self, pid, argv, cwd, cgroup="0::/user.slice/app.slice/app-x.scope\n", exe="/usr/bin/x"):
        d = os.path.join(self.root, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        with open(os.path.join(d, "cgroup"), "w") as f:
            f.write(cgroup)
        os.symlink(cwd, os.path.join(d, "cwd"))
        os.symlink(exe, os.path.join(d, "exe"))

    def test_scan(self):
        claude = "/home/alice/.local/share/claude/versions/2.1.283"
        self.mkproc(100, [claude, "remote-control", "--name", "x"], self.proj, exe=claude)
        self.mkproc(101, ["claude", "rc"], self.h.project, exe=claude)
        self.mkproc(102, [claude, "remote-control"], self.proj,
                    cgroup="0::/user.slice/app.slice/cchub-rc@home-x.service\n", exe=claude + " (deleted)")
        self.mkproc(103, ["/bin/bash", "-c", "echo remote-control"], self.proj)
        self.mkproc(104, [claude, "--print", "--sdk-url", "x"], self.proj, exe=claude)
        os.makedirs(os.path.join(self.root, "self"))
        with open(os.path.join(self.root, "self", "cgroup"), "w") as f:
            f.write("0::/user.slice/user-1000.slice/user@1000.service/app.slice/cchub-rc@home-alice-work-projects.service\n")
        pf = ProcFS(self.root)
        self.assertEqual(sorted(p.pid for p in pf.rc_processes()), [100, 101, 102])
        self.assertEqual([p.pid for p in pf.foreign_rc_for(self.proj)], [100])
        self.assertEqual(sorted(p.pid for p in pf.rc_under(self.h.project)), [100, 101, 102])
        self.assertTrue(pf.exe_deleted(102))
        self.assertFalse(pf.exe_deleted(100))
        self.assertIsNone(pf.exe_deleted(999))
        self.assertTrue(pf.pid_in_unit(102, "cchub-rc@home-x.service"))
        self.assertEqual(pf.current_unit(), "cchub-rc@home-alice-work-projects.service")
        self.assertTrue(pf.in_cchub_unit())

    def test_not_in_unit(self):
        os.makedirs(os.path.join(self.root, "self"))
        with open(os.path.join(self.root, "self", "cgroup"), "w") as f:
            f.write("0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-com.anthropic.Claude-4267.scope\n")
        pf = ProcFS(self.root)
        self.assertIsNone(pf.current_unit())
        self.assertFalse(pf.in_cchub_unit())
        with open(os.path.join(self.root, "self", "cgroup"), "w") as f:
            f.write("0::/user.slice/user-1000.slice/user@1000.service/app.slice/cchub-reconcile.service\n")
        self.assertTrue(pf.in_cchub_unit())

    def test_is_rc_argv(self):
        self.assertTrue(is_rc_argv(("/x/claude", "remote-control")))
        self.assertTrue(is_rc_argv(("/x/versions/2.1.283", "rc")))
        self.assertTrue(is_rc_argv(("claude", "--verbose", "remote-control")))
        self.assertFalse(is_rc_argv(("/usr/bin/python3", "remote-control")))
        self.assertFalse(is_rc_argv(("claude", "--print", "rc")))


if __name__ == "__main__":
    unittest.main()
