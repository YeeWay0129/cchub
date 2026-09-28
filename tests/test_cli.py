"""CLI：AC9 全部拒絕案例、new／open／stop／restart／ls／logs／doctor 流程、AC13 並行 new。"""

import contextlib
import io
import json
import os
import subprocess
import threading
import time
import unittest

import helpers
from cchub.cli import git_init, main
from cchub.names import instance_for_dir, unit_for_instance
from cchub.units import read_instance_config, read_registry, write_instance_config, write_instance_state


def run(ctx, *argv):
    rc = main(list(argv), ctx=ctx)
    return rc, ctx.out.getvalue(), ctx.err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.h = helpers.TempHome()
        self.sysd = helpers.FakeSystemd(on_start=helpers.serve_simulator(self.h))
        self.entry_inst = instance_for_dir(self.h.project)
        self.entry = unit_for_instance(self.entry_inst)
        write_instance_config(self.h.paths, self.entry_inst, {"dir": self.h.project, "entry": True, "mode": "auto",
                                                              "capacity": 3, "title": "projects"})
        self.sysd.states[self.entry] = "active"
        self.h.write(".config/systemd/user/cchub-rc@.service", "[Service]\n")   # 假裝已安裝

    def tearDown(self):
        self.h.cleanup()

    def ctx(self, **kw):
        return helpers.make_ctx(self.h, self.sysd, **kw)

    def unit_of(self, path):
        return unit_for_instance(instance_for_dir(path))


class RejectTest(Base):
    """AC9：以下全部被拒，而且沒有任何副作用。"""

    def test_all_rejections(self):
        self.h.mkdir("work", "projects", "foo", ".claude", "worktrees", "w1")
        wt = os.path.join(self.h.project, "foo", ".claude", "worktrees", "w1")
        in_unit = helpers.FakeProcFS(cgroup=helpers.unit_cgroup(self.entry))
        cases = [
            (["new", "../x"], None, "不合規則"),
            (["new", "A"], None, "不合規則"),
            (["new", "entry"], None, "保留字"),
            (["open", "/etc"], None, "不在允許範圍"),
            (["open", "~"], None, "家目錄"),
            (["open", self.h.home], None, "家目錄"),
            (["open", wt], None, "worktree"),
            (["new", "okname", "--mode", "bypassPermissions"], None, "一律拒絕"),
            (["open", "foo", "--mode", "bypassPermissions"], None, "一律拒絕"),
            (["stop", "entry"], None, "入口不能停止"),
            (["stop", "projects"], None, "入口不能停止"),
            (["stop", self.h.project], None, "入口不能停止"),
            (["install"], in_unit, "終端機"),
            (["install", "--dry-run"], in_unit, "終端機"),
            (["uninstall"], in_unit, "終端機"),
        ]
        before = helpers.snapshot(self.h.project)
        for argv, procfs, needle in cases:
            with self.subTest(argv=argv):
                ctx = self.ctx(procfs=procfs) if procfs else self.ctx()
                rc, out, err = run(ctx, *argv)
                self.assertEqual(rc, 1, out + err)
                self.assertIn(needle, err)
        self.assertEqual(helpers.snapshot(self.h.project), before)
        self.assertEqual(self.sysd.cmds("start") + self.sysd.cmds("stop") + self.sysd.cmds("restart"), [])

    def test_help(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(buf):
            main(["--help"], ctx=self.ctx())
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("用法：cchub", buf.getvalue())


class NewTest(Base):
    def test_new_success(self):
        ctx = self.ctx(stdin=io.StringIO("做記帳工具\n"))
        rc, out, err = run(ctx, "new", "ledger", "--brief-stdin", "--title", "記帳")
        self.assertEqual(rc, 0, out + err)
        d = os.path.join(self.h.project, "ledger")
        lines = out.strip().splitlines()
        first = next(x for x in lines if x.startswith("✅"))
        self.assertEqual(first, "✅ ledger 已上線：到 Claude App → Code → 這台電腦的卡片 → 選「ledger」開新 session")
        self.assertEqual(lines[-1], "備用網址：https://claude.ai/code?environment=env_TEST123")   # 環境網址（N5）
        with open(os.path.join(d, "CLAUDE.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("## 初始需求", md)
        self.assertIn("做記帳工具", md)
        self.assertIn("# 記帳", md)
        self.assertTrue(os.path.isdir(os.path.join(d, ".git")))
        self.assertEqual(subprocess.run(["git", "-C", d, "status"], capture_output=True).returncode, 0)
        cj = self.h.read_claude_json()
        self.assertIs(cj["projects"][d]["hasTrustDialogAccepted"], True)
        reg = read_registry(self.h.paths)
        self.assertEqual(reg["trust_keys"], [d])
        self.assertEqual(reg["pending_new"], {})
        self.assertEqual(reg["projects"][d]["created_by"], "cchub new")
        icfg = read_instance_config(self.h.paths, instance_for_dir(d))
        self.assertEqual((icfg["mode"], icfg["capacity"], icfg["entry"]), ("auto", 3, False))
        self.assertEqual([c[-1] for c in self.sysd.cmds("start")], [self.unit_of(d)])

    def test_not_installed(self):
        os.unlink(os.path.join(self.h.paths.systemd_user_dir, "cchub-rc@.service"))
        rc, out, err = run(self.ctx(), "new", "ledger")
        self.assertEqual(rc, 1)
        self.assertIn("cchub install", err)
        self.assertFalse(os.path.exists(os.path.join(self.h.project, "ledger")))

    def test_N6_brief_option_removed(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(buf):
            main(["new", "ledger", "--brief", "做記帳工具"], ctx=self.ctx())
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--brief", buf.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.h.project, "ledger")))

    def test_new_existing_refused(self):
        self.h.mkdir("work", "projects", "ledger")
        rc, out, err = run(self.ctx(), "new", "ledger")
        self.assertEqual(rc, 1)
        self.assertIn("已存在", err)
        self.assertEqual(self.sysd.cmds("start"), [])

    def test_new_no_git(self):
        before = self.h.read_claude_json()
        rc, out, err = run(self.ctx(), "new", "notes", "--no-git")
        self.assertEqual(rc, 0, out + err)
        d = os.path.join(self.h.project, "notes")
        self.assertFalse(os.path.exists(os.path.join(d, ".git")))
        self.assertEqual(self.h.read_claude_json(), before)      # 不寫信任
        with open(os.path.join(d, "CLAUDE.md"), encoding="utf-8") as f:
            self.assertIn("沒有 git", f.read())

    def test_new_trust_not_effective_stops_unit(self):
        self.sysd.on_start = helpers.serve_simulator(self.h, status="failed",
                                                     error={"kind": "untrusted", "message": "未受信任"})
        rc, out, err = run(self.ctx(), "new", "ledger")
        self.assertEqual(rc, 1)
        self.assertIn("信任寫入沒有生效", out)
        d = os.path.join(self.h.project, "ledger")
        self.assertEqual([c[-1] for c in self.sysd.cmds("stop")], [self.unit_of(d)])
        self.assertTrue(os.path.isdir(d))                          # 資料夾不自動刪

    def test_new_cap_checked_before_creating(self):
        for i in range(6):
            self.sysd.states[self.unit_of(f"{self.h.project}/p{i}")] = "active"
        rc, out, err = run(self.ctx(), "new", "seventh")
        self.assertEqual(rc, 1)
        self.assertIn("上限", err)
        self.assertFalse(os.path.exists(os.path.join(self.h.project, "seventh")))

    def test_concurrent_new_same_name(self):
        """AC13：兩個 new 同時跑，flock 讓它們排隊，不會兩個都成功。"""
        results = []

        def slow_git(dir_fd):
            time.sleep(0.3)
            git_init(dir_fd)

        def worker():
            ctx = self.ctx(git=slow_git)
            results.append(run(ctx, "new", "race"))

        ts = [threading.Thread(target=worker) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        rcs = sorted(r[0] for r in results)
        self.assertEqual(rcs, [0, 1])
        loser = next(r for r in results if r[0] == 1)
        self.assertIn("已存在", loser[2])
        self.assertEqual(len(self.sysd.cmds("start")), 1)
        self.assertEqual(read_registry(self.h.paths)["trust_keys"], [os.path.join(self.h.project, "race")])

    def test_concurrent_new_is_serialized_by_flock(self):
        """AC13：不同名稱的兩個 new 同時跑，臨界區（建資料夾→git→信任→起單元）不能重疊。拿掉 flock 這個測試會失敗。"""
        spans = []
        lock = threading.Lock()

        def slow_git(dir_fd):
            t0 = time.monotonic()
            time.sleep(0.3)
            git_init(dir_fd)
            with lock:
                spans.append((t0, time.monotonic()))

        results = []
        ts = [threading.Thread(target=lambda n=n: results.append(run(self.ctx(git=slow_git), "new", n)))
              for n in ("alpha", "beta")]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(sorted(r[0] for r in results), [0, 0])
        (a0, a1), (b0, b1) = sorted(spans)
        self.assertLessEqual(a1, b0, f"兩個 new 的臨界區重疊了：{spans}")


class OpenTest(Base):
    def setUp(self):
        super().setUp()
        self.plain = self.h.mkdir("work", "projects", "plain")
        self.mcp = self.h.mkdir("work", "projects", "mcp")
        self.h.write("work/projects/mcp/.mcp.json", "{}")

    def test_open_success(self):
        rc, out, err = run(self.ctx(), "open", "plain")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(out.startswith("✅ plain 已上線：到 Claude App → Code → 這台電腦的卡片 → 選「plain」開新 session"))
        self.assertEqual([c[-1] for c in self.sysd.cmds("start")], [self.unit_of(self.plain)])
        self.assertEqual(read_instance_config(self.h.paths, instance_for_dir(self.plain))["mode"], "auto")

    def test_open_policy_refuses_inherited_mcp(self):
        rc, out, err = run(self.ctx(), "open", "mcp")
        self.assertEqual(rc, 1)
        self.assertIn(".mcp.json", err)
        self.assertEqual(self.sysd.cmds("start"), [])

    def test_open_already_online(self):
        self.sysd.states[self.unit_of(self.plain)] = "active"
        rc, out, err = run(self.ctx(), "open", "plain")
        self.assertEqual(rc, 0)
        self.assertIn("已上線", out)
        self.assertEqual(self.sysd.cmds("start"), [])

    def test_open_foreign_rc(self):
        procfs = helpers.FakeProcFS(rc=[helpers.rc_proc(777, self.plain)])
        rc, out, err = run(self.ctx(procfs=procfs), "open", "plain")
        self.assertEqual(rc, 0)
        self.assertIn("已由其他程序提供", out)
        self.assertIn("777", out)
        self.assertEqual(self.sysd.cmds("start"), [])

    def test_open_409_after_start(self):
        self.sysd.on_start = helpers.serve_simulator(self.h, status="failed",
                                                     error={"kind": "already_served", "message": "409"})
        rc, out, err = run(self.ctx(), "open", "plain")
        self.assertEqual(rc, 0)
        self.assertIn("409", out)
        self.assertEqual([c[-1] for c in self.sysd.cmds("stop")], [self.unit_of(self.plain)])

    def test_open_rule8_registration_failed_already_served(self):
        """N3：伺服器自訂文字的註冊失敗，內容含 already served → 同樣依規則 8 回報並停掉單元。"""
        self.sysd.on_start = helpers.serve_simulator(
            self.h, status="exited", last_error={"kind": "registration_failed", "message": "註冊失敗"},
            last_error_detail="Error: This folder is already served by another process (409).")
        rc, out, err = run(self.ctx(), "open", "plain")
        self.assertEqual(rc, 0)
        self.assertIn("已由其他程序提供", out)
        self.assertIn("稍等一分鐘", out)
        self.assertEqual([c[-1] for c in self.sysd.cmds("stop")], [self.unit_of(self.plain)])

    def test_open_registration_failed_other_is_not_rule8(self):
        self.sysd.on_start = helpers.serve_simulator(
            self.h, status="exited", last_error={"kind": "registration_failed", "message": "註冊失敗"},
            last_error_detail="Error: Registration: Access denied (403).")
        rc, out, err = run(self.ctx(wait_seconds=0.2), "open", "plain")
        self.assertEqual(self.sysd.cmds("stop"), [])
        self.assertNotIn("已由其他程序提供", out)

    def test_open_starting_and_failed(self):
        self.sysd.on_start = helpers.serve_simulator(self.h, status="starting")
        rc, out, err = run(self.ctx(wait_seconds=0.2), "open", "plain")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("⏳ plain 還在啟動中"))
        self.sysd.states[self.unit_of(self.plain)] = "inactive"
        self.sysd.on_start = helpers.serve_simulator(self.h, status="failed",
                                                     error={"kind": "auth", "message": "CLI 未登入"},
                                                     recent=[f"l{i}" for i in range(15)])
        rc, out, err = run(self.ctx(), "open", "plain")
        self.assertEqual(rc, 1)
        self.assertIn("❌ plain 啟動失敗：CLI 未登入", out)
        self.assertIn("  l14", out)
        self.assertNotIn("  l4\n", out)                    # 只附最後 10 行

    def test_seventh_open_rejected(self):
        for i in range(6):
            self.sysd.states[self.unit_of(f"{self.h.project}/p{i}")] = "active"
        rc, out, err = run(self.ctx(), "open", "plain")
        self.assertEqual(rc, 1)
        self.assertIn("上限 6", err)
        self.assertEqual(self.sysd.cmds("start"), [])


class StopRestartTest(Base):
    def setUp(self):
        super().setUp()
        self.plain = self.h.mkdir("work", "projects", "plain")
        self.punit = self.unit_of(self.plain)
        write_instance_config(self.h.paths, instance_for_dir(self.plain),
                              {"dir": self.plain, "mode": "auto", "capacity": 3, "entry": False})

    def test_stop(self):
        self.sysd.states[self.punit] = "active"
        rc, out, err = run(self.ctx(), "stop", "plain")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c[-1] for c in self.sysd.cmds("stop")], [self.punit])

    def test_stop_self_is_delayed(self):
        self.sysd.states[self.punit] = "active"
        ctx = self.ctx(procfs=helpers.FakeProcFS(cgroup=helpers.unit_cgroup(self.punit)))
        rc, out, err = run(ctx, "stop", "plain")
        self.assertEqual(rc, 0, err)
        self.assertIn(["systemd-run", "--user", "--on-active=15s", "systemctl", "--user", "stop", self.punit],
                      self.sysd.calls)
        self.assertEqual(self.sysd.cmds("stop"), [])

    def test_restart_entry_in_unit_is_delayed(self):
        other = self.unit_of(f"{self.h.project}/other")
        for cg in (helpers.unit_cgroup(self.entry), helpers.unit_cgroup(other)):
            with self.subTest(cgroup=cg):
                self.sysd.calls.clear()
                rc, out, err = run(self.ctx(procfs=helpers.FakeProcFS(cgroup=cg)), "restart", "entry")
                self.assertEqual(rc, 0, err)
                self.assertEqual(self.sysd.calls, [["systemd-run", "--user", "--on-active=15s", "systemctl",
                                                    "--user", "restart", self.entry]])
                self.assertIn("15 秒後", out)

    def test_restart_entry_from_terminal(self):
        rc, out, err = run(self.ctx(), "restart", "entry")
        self.assertEqual(rc, 0, err)
        self.assertEqual([c[-1] for c in self.sysd.cmds("restart")], [self.entry])
        self.assertIn("✅ projects 已上線", out)

    def test_restart_unmanaged(self):
        self.h.mkdir("work", "projects", "unmanaged")
        rc, out, err = run(self.ctx(), "restart", "unmanaged")
        self.assertEqual(rc, 1)
        self.assertIn("cchub open", err)


class InfoTest(Base):
    def test_ls_json_and_text(self):
        plain = self.h.mkdir("work", "projects", "plain")
        inst = instance_for_dir(plain)
        write_instance_config(self.h.paths, inst, {"dir": plain, "mode": "auto", "capacity": 3, "entry": False})
        self.sysd.states[self.unit_of(plain)] = "failed"
        write_instance_state(self.h.paths, inst, {"status": "failed", "error": {"kind": "already_served",
                                                                               "message": "已由其他程序提供"}})
        write_instance_state(self.h.paths, self.entry_inst, {"status": "ready", "serve_started_at": time.time() - 3700,
                                                             "warnings": ["舊版警告"]})
        rc, out, err = run(self.ctx(), "ls", "--json")
        rows = json.loads(out)
        self.assertEqual([r["name"] for r in rows], ["projects", "plain"])
        self.assertTrue(rows[0]["entry"])
        self.assertEqual(rows[1]["error"]["kind"], "already_served")
        rc, out, err = run(self.ctx(), "ls")
        self.assertIn("✅ 上線  projects（入口），已上線 1時1分", out)
        self.assertIn("❌ 失敗：已由其他程序提供", out)          # AC14：ls 顯示原因
        self.assertIn("⚠️ 舊版警告", out)

    def test_N3_ls_shows_reason_for_stopped_units(self):
        """N3：停止或失敗的單元，ls（含 --json）顯示 last_error 與 last_error_detail 的可讀原因。"""
        cases = {
            "busy": ({"status": "stopped", "error": None, "last_error": {"kind": "already_served"}},
                     "inactive", "⛔ 停止：這個資料夾已由其他程序提供（你可能手動開了 claude rc）"),
            "reg": ({"status": "stopped", "last_error": {"kind": "registration_failed"},
                     "last_error_detail": "Error: Registration: Access denied (403)."},
                    "inactive", "⛔ 停止：註冊失敗：Error: Registration: Access denied (403)."),
            "dead": ({"status": "failed", "error": {"kind": "auth", "message": "CLI 未登入：回電腦執行 claude auth login",
                                                    "permanent": True}},
                     "failed", "❌ 失敗：CLI 未登入：回電腦執行 claude auth login"),
        }
        for name, (st, act, _) in cases.items():
            d = self.h.mkdir("work", "projects", name)
            inst = instance_for_dir(d)
            write_instance_config(self.h.paths, inst, {"dir": d, "mode": "auto", "capacity": 3, "entry": False})
            write_instance_state(self.h.paths, inst, st)
            self.sysd.states[self.unit_of(d)] = act
        rc, out, err = run(self.ctx(), "ls")
        for name, (_, _, want) in cases.items():
            self.assertIn(f"{want}  {name}", out)
        rows = {r["name"]: r for r in json.loads(run(self.ctx(), "ls", "--json")[1])}
        self.assertEqual(rows["busy"]["reason"], "這個資料夾已由其他程序提供（你可能手動開了 claude rc）")
        self.assertEqual(rows["reg"]["last_error_detail"], "Error: Registration: Access denied (403).")
        self.assertEqual(rows["reg"]["reason"], "註冊失敗：Error: Registration: Access denied (403).")

    def test_logs(self):
        self.sysd.journal[self.entry] = "2026-09-28T12:00:00+0800 host cchub[1]: [狀態] Ready · project · main\n"
        rc, out, err = run(self.ctx(), "logs", "entry", "-n", "5")
        self.assertEqual(rc, 0)
        self.assertIn("[狀態] Ready", out)
        self.assertIn(["journalctl", "--user", "-u", self.entry, "-n", "5", "--no-pager", "-o", "short-iso"],
                      self.sysd.calls)
        self.sysd.journal.clear()
        write_instance_state(self.h.paths, self.entry_inst, {"recent": ["a", "b", "c"]})
        rc, out, err = run(self.ctx(), "logs", "entry", "-n", "2")
        self.assertTrue(out.strip().endswith("b\nc"))

    def test_doctor(self):
        repo = self.h.mkdir("work", "projects", "repo")
        subprocess.run(["git", "init", "-q", repo], check=True)
        self.h.mkdir("work", "projects", "hooked")
        self.h.write("work/projects/hooked/.claude/settings.json", '{"hooks": {"Stop": [1]}}')
        rc, out, err = run(self.ctx(), "doctor")
        self.assertIn("未受信任的專案", out)
        self.assertIn(repo, out)
        self.assertIn("hooked", out)
        self.assertIn("含 hooks", out)
        self.assertIn("Remote Control 一次性同意（F9）：已回答", out)


if __name__ == "__main__":
    unittest.main()
