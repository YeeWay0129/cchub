"""install／uninstall：dry-run 完全不動檔案；settings.json 最小 diff 與備份命名；uninstall 還原。

真安裝只在暫存家目錄＋假的 systemctl 上跑，不碰真實系統。
"""

import difflib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

import helpers
from cchub import install
from cchub.cli import main
from cchub.install import (ASK_RULES, INSTALL_MARKER, add_ask_rules, build_install_plan, detect_format,
                           permissions_diff, plan_settings_add, settings_backup_path, JsonFormat)
from cchub.names import instance_for_dir, unit_for_instance
from cchub.paths import load_config
from cchub.units import read_instance_config, read_registry

SETTINGS = {
    "permissions": {"allow": ["Bash(journalctl *)", "Bash(ls *)"]},
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/local/bin/example-hook --pre-tool"}]}]},
    "enabledPlugins": {"a@b": True},
    "theme": "dark",
    "autoMode": {"enabled": True},
}


def settings_text(data=SETTINGS):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def only_insertions(a: str, b: str) -> bool:
    sm = difflib.SequenceMatcher(a=a.splitlines(), b=b.splitlines())
    return all(tag in ("equal", "insert") for tag, *_ in sm.get_opcodes())


class InstallBase(unittest.TestCase):
    def setUp(self):
        self.h = helpers.TempHome()
        self.settings = self.h.write(".claude/settings.json", settings_text())
        self.sysd = helpers.FakeSystemd()
        helpers.install_fake_claude(self.h, version="2.1.290")

    def tearDown(self):
        self.h.cleanup()

    def ctx(self, **kw):
        kw.setdefault("isatty", lambda: True)
        kw.setdefault("ask", lambda prompt: "yes")          # 注入確認函式（沒有可以跳過確認的旗標）
        return helpers.make_ctx(self.h, self.sysd, **kw)


class DryRunTest(InstallBase):
    def test_dry_run_changes_nothing(self):
        before = helpers.snapshot(self.h.tmp)
        ctx = self.ctx()
        rc = main(["install", "--dry-run"], ctx=ctx)
        out = ctx.out.getvalue()
        self.assertEqual(rc, 0, ctx.err.getvalue())
        self.assertEqual(helpers.snapshot(self.h.tmp), before)
        self.assertEqual([c for c in self.sysd.calls if c[0] == "systemctl" and c[2] not in ("show", "list-units", "is-enabled")], [])
        for needle in ("[1/7] 複製程式到 ~/.local/share/cchub", "[2/7] 建立 ~/.local/bin/cchub",
                       "ExecStart=" + os.path.join(self.h.home, ".local/share/cchub/bin/cchub") + " _serve %i",
                       "RestartPreventExitStatus=3", "OnUnitActiveSec=5min",
                       "~/.claude/skills/cchub/SKILL.md",
                       "先備份到 ~/.claude/backups/settings.json.backup." + time.strftime("%Y%m%d"),
                       "+    \"ask\": [", "+      \"Bash(cchub new *)\",",
                       "enable --now " + unit_for_instance(instance_for_dir(self.h.project)) + " cchub-reconcile.timer",
                       "[7/7] 執行 cchub doctor"):
            self.assertIn(needle, out)
        self.assertNotIn("example-hook", out)       # 只印 permissions 區塊，不印整份檔

    def test_dry_run_warns_manual_rc(self):
        procfs = helpers.FakeProcFS(rc=[helpers.rc_proc(555, self.h.project),
                                        helpers.rc_proc(556, os.path.join(self.h.project, "g0"))])
        ctx = self.ctx(procfs=procfs)
        self.assertEqual(main(["install", "--dry-run"], ctx=ctx), 0)
        out = ctx.out.getvalue()
        self.assertIn("pid 555", out)
        self.assertIn("Ctrl+C", out)
        self.assertIn("pid 556", out)

    def test_uninstall_dry_run_changes_nothing(self):
        before = helpers.snapshot(self.h.tmp)
        ctx = self.ctx()
        self.assertEqual(main(["uninstall", "--dry-run"], ctx=ctx), 0)
        self.assertEqual(helpers.snapshot(self.h.tmp), before)


class RealInstallTest(InstallBase):
    """真安裝流程，但只在暫存家目錄＋假的 systemctl。"""

    def test_install_then_uninstall(self):
        p = self.h.paths
        orig = settings_text()
        ctx = self.ctx()
        rc = main(["install"], ctx=ctx)
        self.assertEqual(rc, 0, ctx.out.getvalue() + ctx.err.getvalue())
        # 1. 複製
        self.assertTrue(os.path.exists(os.path.join(p.install_dir, INSTALL_MARKER)))
        self.assertTrue(os.path.exists(os.path.join(p.install_dir, "cchub", "cli.py")))
        self.assertTrue(os.access(p.install_bin, os.X_OK))
        self.assertFalse(os.path.exists(os.path.join(p.install_dir, "cchub", "__pycache__")))
        # 2. symlink
        self.assertEqual(os.readlink(p.bin_link), p.install_bin)
        # 3. 單元檔
        with open(os.path.join(p.systemd_user_dir, "cchub-rc@.service"), encoding="utf-8") as f:
            unit = f.read()
        self.assertIn(f"ExecStart={p.install_bin} _serve %i", unit)
        self.assertIn(f"Environment=PATH={self.h.home}/.local/bin:/usr/local/bin:/usr/bin:/bin", unit)
        self.assertIn("RestartPreventExitStatus=3", unit)
        self.assertNotIn("@@", unit)
        self.assertEqual(len(self.sysd.cmds("daemon-reload")), 1)
        # 4. skill
        with open(p.skill_file, encoding="utf-8") as f:
            self.assertTrue(f.read().startswith("---\nname: cchub\n"))
        # 5. settings：只新增 ask 規則，純插入；備份檔名照維護協議
        with open(self.settings, encoding="utf-8") as f:
            new = f.read()
        self.assertTrue(only_insertions(orig, new))
        data = json.loads(new)
        self.assertEqual(data["permissions"]["ask"], ASK_RULES)
        del data["permissions"]["ask"]
        self.assertEqual(data, SETTINGS)
        backup = os.path.join(p.backups_dir, "settings.json.backup." + time.strftime("%Y%m%d"))
        with open(backup, encoding="utf-8") as f:
            self.assertEqual(f.read(), orig)
        # 6. 設定、入口實例、enable
        cfg = load_config(p)
        entry_inst = instance_for_dir(self.h.project)
        self.assertTrue(read_instance_config(p, entry_inst)["entry"])
        self.assertIn(["systemctl", "--user", "enable", "--now", unit_for_instance(entry_inst), "cchub-reconcile.timer"],
                      self.sysd.calls)
        self.assertEqual(cfg.entry_dir, self.h.project)
        reg = read_registry(p)
        self.assertEqual(reg["install"]["settings"]["rules_added"], ASK_RULES)
        self.assertTrue(reg["install"]["settings"]["created_ask"])
        # 7. doctor 有跑
        self.assertIn("cchub doctor", ctx.out.getvalue())

        # 再裝一次：規則已存在 → settings 不再修改、不再備份
        ctx2 = self.ctx()
        self.assertEqual(main(["install"], ctx=ctx2), 0, ctx2.err.getvalue())
        with open(self.settings, encoding="utf-8") as f:
            self.assertEqual(f.read(), new)
        self.assertFalse(os.path.exists(backup + "-2"))

        # uninstall：settings 一字不差還原；備份用 -2
        ctx3 = self.ctx()
        self.assertEqual(main(["uninstall"], ctx=ctx3), 0, ctx3.err.getvalue())
        with open(self.settings, encoding="utf-8") as f:
            self.assertEqual(f.read(), orig)
        with open(backup + "-2", encoding="utf-8") as f:
            self.assertEqual(f.read(), new)
        self.assertFalse(os.path.lexists(p.bin_link))
        self.assertFalse(os.path.exists(p.install_dir))
        self.assertFalse(os.path.exists(p.skill_file))
        for n in install.UNIT_TEMPLATES:
            self.assertFalse(os.path.exists(os.path.join(p.systemd_user_dir, n)))
        self.assertIn(["systemctl", "--user", "disable", "--now", unit_for_instance(entry_inst), "cchub-reconcile.timer"],
                      self.sysd.calls)

    def test_uninstall_revokes_trust_keys(self):
        ctx = self.ctx()
        self.assertEqual(main(["install"], ctx=ctx), 0)
        self.sysd.on_start = helpers.serve_simulator(self.h)
        ctx = self.ctx()
        self.assertEqual(main(["new", "ledger"], ctx=ctx), 0, ctx.err.getvalue())
        d = os.path.join(self.h.project, "ledger")
        self.assertTrue(self.h.read_claude_json()["projects"][d]["hasTrustDialogAccepted"])
        ctx = self.ctx()
        self.assertEqual(main(["uninstall"], ctx=ctx), 0, ctx.err.getvalue())
        self.assertFalse(self.h.read_claude_json()["projects"][d]["hasTrustDialogAccepted"])
        self.assertTrue(os.path.isdir(d))            # 建出來的專案資料夾不刪

    def test_requires_tty_and_confirmation(self):
        ctx = self.ctx(isatty=lambda: False)
        self.assertEqual(main(["install"], ctx=ctx), 1)
        self.assertIn("終端機", ctx.err.getvalue())
        ctx = self.ctx(ask=lambda prompt: "no")
        self.assertEqual(main(["install"], ctx=ctx), 1)
        self.assertIn("已取消", ctx.out.getvalue())
        self.assertFalse(os.path.exists(self.h.paths.install_dir))

    def test_blockers_abort_without_changes(self):
        before = helpers.snapshot(self.h.tmp)
        procfs = helpers.FakeProcFS(rc=[helpers.rc_proc(555, self.h.project)])
        ctx = self.ctx(procfs=procfs)
        self.assertEqual(main(["install"], ctx=ctx), 1)
        self.assertIn("Ctrl+C", ctx.err.getvalue())
        self.assertEqual(helpers.snapshot(self.h.tmp), before)
        # ~/.local/bin/cchub 已是別的檔案
        self.h.write(".local/bin/cchub", "someone else's")
        before = helpers.snapshot(self.h.tmp)
        ctx = self.ctx()
        self.assertEqual(main(["install"], ctx=ctx), 1)
        self.assertIn("不覆蓋", ctx.err.getvalue())
        self.assertEqual(helpers.snapshot(self.h.tmp), before)

    def test_unreproducible_format_is_blocker(self):
        with open(self.settings, "w", encoding="utf-8") as f:
            f.write('{"permissions":{"allow":[]},  "theme": "dark"}')
        before = helpers.snapshot(self.h.tmp)
        ctx = self.ctx()
        self.assertEqual(main(["install"], ctx=ctx), 1)
        self.assertIn("無法一字不差", ctx.err.getvalue())
        self.assertEqual(helpers.snapshot(self.h.tmp), before)


class ExistingRulesAndFilesTest(InstallBase):
    """uninstall 只移除 registry 記錄為 cchub 新增的規則；既有的 skill／單元檔先備份、只刪 hash 相符的。"""

    def read_settings(self):
        with open(self.settings, "rb") as f:
            return f.read()

    def test_preexisting_rules_are_not_removed(self):
        pre = dict(SETTINGS, permissions={"ask": ASK_RULES + ["Bash(rm -rf *)"], "allow": ["Bash(ls *)"]})
        with open(self.settings, "w", encoding="utf-8") as f:
            f.write(settings_text(pre))
        before = self.read_settings()
        self.assertEqual(main(["install"], ctx=self.ctx()), 0)
        self.assertEqual(self.read_settings(), before)                   # 規則原本就在 → 不改
        self.assertEqual(read_registry(self.h.paths)["install"]["settings"]["rules_added"], [])
        c = self.ctx()
        self.assertEqual(main(["uninstall"], ctx=c), 0, c.err.getvalue())
        self.assertEqual(self.read_settings(), before)                   # uninstall 也不能刪使用者自己的規則
        self.assertIn("cchub 沒有新增任何規則", c.out.getvalue())

    def test_missing_registry_deletes_nothing(self):
        self.assertEqual(main(["install"], ctx=self.ctx()), 0)
        installed = self.read_settings()
        os.unlink(self.h.paths.registry_file)
        c = self.ctx()
        self.assertEqual(main(["uninstall"], ctx=c), 0, c.err.getvalue())
        self.assertEqual(self.read_settings(), installed)                # 沒紀錄 → 什麼都不刪
        self.assertIn("請手動檢查", c.out.getvalue())
        self.assertTrue(os.path.exists(self.h.paths.skill_file))         # 沒有 sha256 紀錄 → 保留
        self.assertIn("沒有安裝紀錄", c.out.getvalue())

    def test_existing_skill_backed_up_and_hash_guarded(self):
        p = self.h.paths
        os.makedirs(p.skill_dir)
        with open(p.skill_file, "w") as f:
            f.write("USER OWN SKILL")
        c = self.ctx()
        self.assertEqual(main(["install", "--dry-run"], ctx=c), 0)
        self.assertIn("已存在而且內容不同", c.out.getvalue())
        c = self.ctx()
        self.assertEqual(main(["install"], ctx=c), 0, c.err.getvalue())
        bk = os.path.join(p.backups_dir, "skills-cchub-SKILL.md.backup." + time.strftime("%Y%m%d"))
        with open(bk) as f:
            self.assertEqual(f.read(), "USER OWN SKILL")
        self.assertIn("先備份到", c.out.getvalue())
        with open(p.skill_file, "a") as f:                                # 使用者安裝後又改了 → uninstall 保留
            f.write("\nuser edit")
        c = self.ctx()
        self.assertEqual(main(["uninstall"], ctx=c), 0, c.err.getvalue())
        self.assertTrue(os.path.exists(p.skill_file))
        self.assertIn("sha256 不符", c.out.getvalue())
        self.assertIn(os.path.basename(bk), c.out.getvalue())             # 告訴使用者原檔備份在哪

    def test_unit_file_backup_and_delete_when_hash_matches(self):
        p = self.h.paths
        unit = os.path.join(p.systemd_user_dir, "cchub-rc@.service")
        os.makedirs(p.systemd_user_dir)
        with open(unit, "w") as f:
            f.write("[Service]\nExecStart=/old\n")
        self.assertEqual(main(["install"], ctx=self.ctx()), 0)
        with open(unit + ".bak") as f:
            self.assertEqual(f.read(), "[Service]\nExecStart=/old\n")
        rec = read_registry(p)["install"]["files"][unit]
        self.assertEqual(rec["backup"], unit + ".bak")
        self.assertEqual(len(rec["sha256"]), 64)
        self.assertEqual(main(["uninstall"], ctx=self.ctx()), 0)
        self.assertFalse(os.path.exists(unit))                          # 是 cchub 寫的（hash 相符）→ 刪
        self.assertTrue(os.path.exists(unit + ".bak"))                  # 備份留著


class SettingsUnitTest(unittest.TestCase):
    def test_backup_naming(self):
        h = helpers.TempHome()
        try:
            d = h.paths.backups_dir
            now = time.time()
            base = os.path.join(d, "settings.json.backup." + time.strftime("%Y%m%d", time.localtime(now)))
            self.assertEqual(settings_backup_path(d, now), base)
            open(base, "w").close()
            self.assertEqual(settings_backup_path(d, now), base + "-2")
            open(base + "-2", "w").close()
            self.assertEqual(settings_backup_path(d, now), base + "-3")
        finally:
            h.cleanup()

    def test_add_rules_minimal(self):
        for start in ({}, {"theme": "x"}, {"permissions": {}}, {"permissions": {"ask": ["Bash(x)"]}},
                      {"permissions": {"allow": ["a"], "deny": ["b"]}, "z": 1}):
            with self.subTest(start=start):
                before = settings_text(start)
                new, added, _, _ = add_ask_rules(start, ASK_RULES)
                self.assertEqual(added, ASK_RULES)
                self.assertTrue(only_insertions(before, settings_text(new)) or start == {} or
                                start == {"permissions": {}})
                self.assertEqual(new["permissions"]["ask"][:6], ASK_RULES)
        new, added, _, _ = add_ask_rules({"permissions": {"ask": ASK_RULES[:2]}}, ASK_RULES)
        self.assertEqual(added, ASK_RULES[2:])
        with self.assertRaises(Exception):
            add_ask_rules({"permissions": None}, ASK_RULES)

    def test_detect_format_and_diff_line_numbers(self):
        text = settings_text()
        fmt = detect_format(text, SETTINGS)
        self.assertEqual(fmt, JsonFormat(2, True))
        self.assertIsNone(detect_format('{"a":  1}', {"a": 1}))          # 排版重現不了 → 不自動改
        new, _, _, _ = add_ask_rules(SETTINGS, ASK_RULES)
        diff = permissions_diff(text, SETTINGS, fmt.dump(new), new, fmt, "s.json")
        self.assertEqual(diff[0], "--- s.json（修改前）")
        self.assertEqual(diff[2], "@@ -2,4 +2,12 @@")
        self.assertIn('+    "ask": [', diff)
        self.assertTrue(all("example-hook" not in ln for ln in diff))


if __name__ == "__main__":
    unittest.main()


class ProjectsRootOptionTest(unittest.TestCase):
    """路徑不再寫死：沒有 config.json 時 install 必須帶 --projects-root；已有 config.json 時行為不變。"""

    def setUp(self):
        self.h = helpers.TempHome(config=False)
        self.h.write(".claude/settings.json", settings_text())
        helpers.install_fake_claude(self.h)
        self.sysd = helpers.FakeSystemd()

    def tearDown(self):
        self.h.cleanup()

    def ctx(self, **kw):
        kw.setdefault("isatty", lambda: True)
        kw.setdefault("ask", lambda prompt: "yes")
        return helpers.make_ctx(self.h, self.sysd, **kw)

    def run_main(self, *argv):
        c = self.ctx()
        rc = main(list(argv), ctx=c)
        return rc, c.out.getvalue(), c.err.getvalue()

    def read_config(self):
        with open(self.h.paths.config_file, encoding="utf-8") as f:
            return json.load(f)

    def test_projects_root_required_without_config(self):
        before = helpers.snapshot(self.h.tmp)
        for argv in (["install", "--dry-run"], ["install"]):
            with self.subTest(argv=argv):
                rc, out, err = self.run_main(*argv)
                self.assertEqual(rc, 1)
                self.assertIn("--projects-root", err)
                self.assertIn("cchub install --projects-root", err)
        self.assertEqual(helpers.snapshot(self.h.tmp), before)

    def test_defaults_derived_from_projects_root(self):
        """--projects-root 之外都不給：allowed_roots＝[projects_root]、entry_dir＝projects_root。"""
        before = helpers.snapshot(self.h.tmp)
        rc, out, err = self.run_main("install", "--dry-run", "--projects-root", self.h.project)
        self.assertEqual(rc, 0, err)
        self.assertEqual(helpers.snapshot(self.h.tmp), before)            # dry-run 不寫任何檔
        entry_unit = unit_for_instance(instance_for_dir(self.h.project))
        self.assertIn(f"enable --now {entry_unit} cchub-reconcile.timer", out)
        self.assertIn('"allowed_roots": [\n            "' + self.h.project + '"\n          ]', out)
        rc, out, err = self.run_main("install", "--projects-root", self.h.project)
        self.assertEqual(rc, 0, err)
        cfg = self.read_config()
        self.assertEqual((cfg["projects_root"], cfg["allowed_roots"], cfg["entry_dir"]),
                         (self.h.project, [self.h.project], self.h.project))
        self.assertEqual(read_instance_config(self.h.paths, instance_for_dir(self.h.project))["dir"], self.h.project)

    def test_tilde_and_explicit_options(self):
        hub = self.h.mkdir("work", "hub")
        rc, out, err = self.run_main("install", "--projects-root", "~/work/projects", "--allowed-root", "~/work",
                                     "--allowed-root", self.h.project, "--entry-dir", hub)
        self.assertEqual(rc, 0, err)
        cfg = self.read_config()
        self.assertEqual(cfg["projects_root"], self.h.project)
        self.assertEqual(cfg["allowed_roots"], [self.h.work, self.h.project])
        self.assertEqual(cfg["entry_dir"], hub)
        self.assertIn(["systemctl", "--user", "enable", "--now", unit_for_instance(instance_for_dir(hub)),
                       "cchub-reconcile.timer"], self.sysd.calls)

    def test_missing_or_invalid_directories_refused(self):
        nope = os.path.join(self.h.home, "nope")
        outside = os.path.realpath(tempfile.mkdtemp(prefix="cchub-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        cases = [
            (["--projects-root", nope], "不存在"),
            (["--projects-root", self.h.project, "--allowed-root", nope], "不存在"),
            (["--projects-root", self.h.project, "--entry-dir", nope], "不存在"),
            (["--projects-root", outside], "家目錄底下"),
            (["--projects-root", self.h.project, "--allowed-root", self.h.work, "--entry-dir", self.h.home], "entry_dir"),
        ]
        before = helpers.snapshot(self.h.tmp)
        for opts, needle in cases:
            with self.subTest(opts=opts):
                rc, out, err = self.run_main("install", "--dry-run", *opts)
                self.assertEqual(rc, 1)
                self.assertIn(needle, err)
        self.assertFalse(os.path.exists(nope))                            # 不自動建立
        self.assertEqual(helpers.snapshot(self.h.tmp), before)

    def test_existing_config_is_used_unchanged(self):
        """使用者機器上已經有 config.json：重裝（dry-run 與真裝）都沿用它，檔案一字不變。"""
        existing = {"projects_root": self.h.project, "allowed_roots": [self.h.project, self.h.work],
                    "entry_dir": self.h.project, "default_mode": "auto", "entry_mode": "auto",
                    "default_capacity": 3, "max_servers": 6, "probe_host": "api.anthropic.com", "probe_port": 443}
        self.h.write_config(existing)
        with open(self.h.paths.config_file, "rb") as f:
            before = f.read()
        rc, out, err = self.run_main("install", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("config.json：已存在，不動", out)
        rc, out, err = self.run_main("install")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_main("install", "--projects-root", self.h.project)   # 選項與既有相同 → 可以
        self.assertEqual(rc, 0, err)
        with open(self.h.paths.config_file, "rb") as f:
            self.assertEqual(f.read(), before)
        other = self.h.mkdir("work", "other")
        for opts in (["--projects-root", other], ["--allowed-root", self.h.project], ["--entry-dir", other]):
            with self.subTest(opts=opts):
                rc, out, err = self.run_main("install", "--dry-run", *opts)
                self.assertEqual(rc, 1)
                self.assertIn("已經有設定檔", err)
        with open(self.h.paths.config_file, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_other_commands_without_config(self):
        for argv in (["ls"], ["open", "x"], ["doctor"]):
            with self.subTest(argv=argv):
                rc, out, err = self.run_main(*argv)
                self.assertEqual(rc, 1)
                self.assertIn("cchub install --projects-root", out + err)
        rc, out, err = self.run_main("uninstall", "--dry-run")           # 沒有設定也能看 uninstall 會做什麼
        self.assertEqual(rc, 0, err)
        self.assertIn("找不到入口單元名稱", out)

    def test_skill_rendered_with_real_paths(self):
        from cchub.install import render_skill
        from cchub.paths import config_from_options
        cfg = config_from_options(self.h.home, self.h.project, [self.h.project, self.h.work])
        text = render_skill(self.h.paths, cfg)
        self.assertNotIn("@@", text)
        self.assertIn("在 `~/work/projects/<名稱>` 建資料夾", text)
        self.assertIn("入口（`~/work/projects`，卡片上的「projects」）不能停止", text)
        rc, out, err = self.run_main("install", "--projects-root", self.h.project)
        self.assertEqual(rc, 0, err)
        with open(self.h.paths.skill_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)

    def test_skill_entry_label_uses_git_root(self):
        from cchub.install import entry_label
        repo = self.h.mkdir("work", "projects", "hub-repo")
        subprocess.run(["git", "init", "-q", repo], check=True)
        sub = self.h.mkdir("work", "projects", "hub-repo", "entry")
        self.assertEqual(entry_label(sub), "hub-repo")
        self.assertEqual(entry_label(self.h.project), "projects")
