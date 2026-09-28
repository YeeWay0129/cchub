"""獨立驗收（verify-report.md）的重現腳本改寫成 unittest：D1–D12 各一組對抗性測試。

這些測試只用穩定的入口（main()、Supervisor、SKILL.md、install 流程），
所以也能拿去跑修正前的版本，確認「修正前會失敗、修正後通過」。
"""

import io
import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import sys
import time
import unittest

import helpers
from cchub import trust
from cchub.cli import main
from cchub.install import ASK_RULES
from cchub.names import instance_for_dir, validate_new_name
from cchub.paths import Paths
from cchub.serve import PERMANENT_EXIT, Supervisor
from cchub.units import read_instance_state, read_registry, write_instance_config
from cchub.util import CchubError

ESC = "\x1b"
SKILL = os.path.join(helpers.REPO, "templates", "SKILL.md")


def osc8(url, text):
    return f"{ESC}]8;;{url}{ESC}\\{text}{ESC}]8;;{ESC}\\"


def dim(s):
    return f"{ESC}[2m{s}{ESC}[22m"


def block_multi(title, summary, url="https://claude.ai/code/session_EXAMPLEreal"):
    return (f"{ESC}[36m·✔︎·{ESC}[39m {ESC}[36mConnected{ESC}[39m{dim(' · ')}{dim('proj')}{dim(' · ')}{dim('main')}\n"
            f"    {dim('Capacity: 1/3 · New sessions will be created in the current directory')}\n"
            f"    {osc8(url, title)}{dim(' ' + summary)}\n")


def block_single(title):
    return (f"{ESC}[36m·✔︎·{ESC}[39m {ESC}[36m{title}{ESC}[39m{dim(' · ')}{dim('proj')}{dim(' · ')}{dim('main')}\n"
            f"    {dim('Capacity: 1/1 · New sessions will be created in the current directory')}\n")


class ServeBase(unittest.TestCase):
    def setUp(self):
        self.h = helpers.TempHome()
        self.dir = self.h.mkdir("work", "projects", "proj")
        self.inst = instance_for_dir(self.dir)
        write_instance_config(self.h.paths, self.inst, {"dir": self.dir, "title": "proj", "mode": "auto",
                                                        "capacity": 3, "entry": False})

    def tearDown(self):
        self.h.cleanup()

    def run_sup(self, inst=None, **kw):
        out = io.StringIO()
        sup = Supervisor(self.h.paths, inst or self.inst, probe=lambda: True, sleep=lambda s: None, out=out,
                         environ={"HOME": self.h.home, "PATH": "/usr/bin:/bin"}, install_signals=False, **kw)
        rc = sup.run()
        return rc, out.getvalue(), read_instance_state(self.h.paths, inst or self.inst)


class D1PermanentClassificationTest(ServeBase):
    """D1【高】：對話內容、過期錯誤、一般錯誤訊息都不能讓伺服器以 exit 3 永久停下。"""

    def test_E_title_error_then_disconnect(self):
        helpers.install_fake_claude(self.h, output=block_multi("Error: Workspace not trusted fix",
                                                               "Reading SECRET-PLAN-7731.md") * 3,
                                    stderr="Error: Remote Control disconnected: network unreachable\n", exit=1)
        rc, out, st = self.run_sup()
        self.assertNotEqual(rc, PERMANENT_EXIT)
        self.assertNotEqual(st.get("status"), "failed")

    def test_B_title_login_words(self):
        helpers.install_fake_claude(self.h, output=block_multi("Error: You must be logged in", "Running npm test") * 2,
                                    exit=1)
        self.assertNotEqual(self.run_sup()[0], PERMANENT_EXIT)

    def test_F_old_401_then_unrelated_exit(self):
        helpers.install_fake_claude(
            self.h, output=block_multi("t", "s") * 50,
            stderr="[09:00:01] Error: Registration: Authentication failed (401). Retrying…\n"
                   "[13:40:00] Error: Remote Control disconnected (network)\n", exit=1)
        self.assertNotEqual(self.run_sup()[0], PERMANENT_EXIT)

    def test_F_window_with_exact_not_logged_in(self):
        """F（加強）：完全吻合的「未登入」在結束前 30 秒內 → 3；不在窗內 → 不是 3（窗縮成 0.3 秒、晚 1 秒結束）。"""
        msg = "Error: You must be logged in to use Remote Control.\n"
        helpers.install_fake_claude(self.h, stderr=msg, exit=1)
        self.assertEqual(self.run_sup()[0], PERMANENT_EXIT)
        helpers.install_fake_claude(self.h, stderr=msg, hang=1.0, exit=1)
        self.assertNotEqual(self.run_sup(perm_window=0.3)[0], PERMANENT_EXIT)

    def test_G_H_generic_already_messages(self):
        for msg in ("[10:00:00] Error: Session spawn failed: EEXIST: file already exists, open '/p/server.log'",
                    "Error: port already in use by dev server"):
            with self.subTest(msg=msg[:40]):
                helpers.install_fake_claude(self.h, stderr=msg + "\n", exit=1)
                self.assertNotEqual(self.run_sup()[0], PERMANENT_EXIT)


class D2WhitelistLeakTest(ServeBase):
    """D2【中】：session 標題、活動摘要不能進 journal／狀態檔；不能注入假的 session 網址。"""

    def test_A_to_D_fixtures(self):
        text = (block_multi("Error: Workspace not trusted fix", "Reading SECRET-PLAN-7731.md")
                + block_single("Connected · https://claude.ai/code/session_EVIL123")
                + block_multi("see https://claude.ai/code/session_EVIL999", "x",
                              url="https://claude.ai/code/session_EVIL888"))
        helpers.install_fake_claude(self.h, output=text * 2, exit=1)
        rc, out, st = self.run_sup()
        dumped = json.dumps(st, ensure_ascii=False)
        for bad in ("SECRET-PLAN-7731", "EVIL", "Workspace not trusted fix"):
            self.assertNotIn(bad, out)
            self.assertNotIn(bad, dumped)
        self.assertFalse(any("EVIL" in u for u in st.get("session_urls") or []))


class D3TrustToctouTest(unittest.TestCase):
    """D3【中】：new 建好資料夾之後、寫信任之前，把路徑換成指向既有資料夾的 symlink（或改名）→ 必須拒絕。

    換的時機用 git 那一步（ctx.git 的包裝）：真的 git init 完成後立刻換，確保情境真的發生。
    """

    def run_new_with_swap(self, swap):
        h = helpers.TempHome()
        self.addCleanup(h.cleanup)
        victim = h.mkdir("secrets")
        target = os.path.join(h.project, "innocent")
        fired = []

        def git_then_swap(dir_fd):
            from cchub.cli import git_init
            git_init(dir_fd)
            swap(target, victim)
            fired.append(True)

        h.write(".config/systemd/user/cchub-rc@.service", "[Service]\n")
        sysd = helpers.FakeSystemd(on_start=helpers.serve_simulator(h))
        ctx = helpers.make_ctx(h, sysd, git=git_then_swap)
        rc = main(["new", "innocent"], ctx)
        self.assertEqual(fired, [True], "換路徑的情境沒有真的發生")
        cj = h.read_claude_json()["projects"]
        return rc, ctx, cj, victim, target, sysd, h

    def test_swap_to_symlink_refused(self):
        def swap(target, victim):
            os.rename(target, target + ".moved")
            os.symlink(victim, target)
        rc, ctx, cj, victim, target, sysd, h = self.run_new_with_swap(swap)
        self.assertEqual(rc, 1)
        self.assertIn("移動或換掉", ctx.err.getvalue())
        for k in (victim, target, target + ".moved"):
            self.assertIsNone((cj.get(k) or {}).get("hasTrustDialogAccepted"))
        self.assertEqual(read_registry(h.paths)["trust_keys"], [])
        self.assertEqual(sysd.cmds("start"), [])

    def test_rename_refused(self):
        def swap(target, victim):
            os.rename(target, target + "-renamed")
        rc, ctx, cj, victim, target, sysd, h = self.run_new_with_swap(swap)
        self.assertEqual(rc, 1)
        self.assertIsNone((cj.get(target + "-renamed") or {}).get("hasTrustDialogAccepted"))
        self.assertEqual(read_registry(h.paths)["trust_keys"], [])


class D4InstallConfirmationTest(unittest.TestCase):
    """D4【中】：沒有旗標能跳過「輸入 yes」；在 Claude Code session 裡（CLAUDECODE）拒絕。"""

    def setUp(self):
        self.h = helpers.TempHome()
        self.settings = self.h.write(".claude/settings.json", '{\n  "theme": "dark"\n}\n')
        helpers.install_fake_claude(self.h)

    def tearDown(self):
        self.h.cleanup()

    def settings_bytes(self):
        with open(self.settings, "rb") as f:
            return f.read()

    def test_yes_flag_cannot_skip_confirmation(self):
        before = self.settings_bytes()
        asked = []
        ctx = helpers.make_ctx(self.h, isatty=lambda: True, ask=lambda p: (asked.append(p), "no")[1])
        try:
            with open(os.devnull, "w") as devnull, __import__("contextlib").redirect_stderr(devnull):
                rc = main(["install", "--yes"], ctx=ctx)
        except SystemExit as e:
            rc = e.code
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.settings_bytes(), before)
        self.assertFalse(os.path.exists(self.h.paths.install_dir))

    def test_claudecode_env_refused(self):
        before = self.settings_bytes()
        ctx = helpers.make_ctx(self.h, isatty=lambda: True, ask=lambda p: "yes", environ={"CLAUDECODE": "1"})
        self.assertIn("environ", type(ctx).__dataclass_fields__, "修正前的 Context 沒有 environ：無法拒絕")
        for cmd in ("install", "uninstall"):
            with self.subTest(cmd=cmd):
                c = helpers.make_ctx(self.h, isatty=lambda: True, ask=lambda p: "yes", environ={"CLAUDECODE": "1"})
                self.assertEqual(main([cmd], ctx=c), 1)
                self.assertIn("自己的終端機", c.err.getvalue())
        self.assertEqual(self.settings_bytes(), before)
        self.assertFalse(os.path.exists(self.h.paths.install_dir))


class D5SkillQuotingTest(unittest.TestCase):
    """D5【中】＋N4：使用者原話不能經過 shell 展開；原文剛好有一行分隔字也不能提早結束。

    照 SKILL.md 的範本組指令（像模型會做的那樣）：分隔字換成新的隨機 8 位十六進位、確認原文沒有一行等於它、
    標題先拿掉引號／反斜線／換行，再用真的 bash 執行（cchub 換成只記錄 argv／stdin 的假程式）。
    """

    SAMPLE = "追蹤 $AAPL 跟 $TSLA 的價格，用 `date` 標時間；它's \"quoted\" \\ back"
    PLACEHOLDER = "<使用者的原話，照抄，不用跳脫任何字元>"

    def skill_block(self):
        with open(SKILL, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn('--brief "', text, "SKILL.md 不應該把原話放進雙引號")
        self.assertNotIn("自動去掉", text, "不能說 cchub 會替你去掉引號（shell 先解析）")
        block = text.split("```bash\n", 1)[1].split("```", 1)[0]
        self.assertTrue(block.startswith("cchub new "), "範例指令必須以 cchub new 開頭（ask 規則比對得到）")
        m = re.search(r"<<'(CCHUB_BRIEF_[0-9a-f]{8})'", block)
        self.assertIsNotNone(m, "分隔字要是 CCHUB_BRIEF_<8 位十六進位>，而且加單引號")
        self.assertEqual(block.rstrip("\n").splitlines()[-1], m.group(1))
        return block, m.group(1)

    def build_command(self, user_text, title="記帳工具"):
        block, example = self.skill_block()
        while True:                                   # 模型每次換新的，並確認原文沒有一行等於它
            delim = "CCHUB_BRIEF_" + secrets.token_hex(4)
            if delim not in user_text.split("\n"):
                break
        clean_title = "".join(ch for ch in title if ch not in "'\"\\\n")
        return (block.replace(example, delim).replace(self.PLACEHOLDER, user_text)
                .replace("--title '記帳工具'", f"--title '{clean_title}'"))

    def run_bash(self, command):
        tmp = os.path.realpath(__import__("tempfile").mkdtemp(prefix="cchub-bash-"))
        try:
            rec = os.path.join(tmp, "rec.json")
            fake = os.path.join(tmp, "cchub")
            with open(fake, "w") as f:
                f.write(f"#!{sys.executable}\nimport json,sys\njson.dump({{'argv': sys.argv[1:], "
                        f"'stdin': sys.stdin.read()}}, open({rec!r}, 'w'))\n")
            os.chmod(fake, 0o755)
            subprocess.run(["bash", "-c", command], cwd=tmp, env={"PATH": f"{tmp}:/usr/bin:/bin"}, timeout=30,
                           stdin=subprocess.DEVNULL, capture_output=True)
            leftovers = sorted(n for n in os.listdir(tmp) if n not in ("cchub", "rec.json"))
            with open(rec) as f:
                return json.load(f), leftovers
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_skill_template_keeps_text_verbatim(self):
        r, leftovers = self.run_bash(self.build_command(self.SAMPLE))
        self.assertEqual(r["stdin"], self.SAMPLE + "\n")
        self.assertEqual(r["argv"][:2], ["new", "ledger"])
        self.assertIn("--brief-stdin", r["argv"])
        self.assertEqual(leftovers, [])

    def test_N4_hostile_briefs_run_nothing(self):
        """N4：原文含整行 CCHUB_BRIEF、$(touch …)、反引號 → 什麼都不執行、原文完整傳入。"""
        briefs = {
            "delim-line": "第一行需求\nCCHUB_BRIEF\ntouch INJECTED-A\n第三行",
            "cmd-subst": "價格 $(touch INJECTED-B) 與 ${HOME}",
            "backticks": "用 `touch INJECTED-C` 標時間",
            "all": "CCHUB_BRIEF\n$(touch INJECTED-D)\n`touch INJECTED-E`\nCCHUB_BRIEF_\n'\"\\",
        }
        for name, text in briefs.items():
            with self.subTest(name=name):
                r, leftovers = self.run_bash(self.build_command(text, title="It's \"記帳\" \\ 工具\n二"))
                self.assertEqual(r["stdin"], text + "\n")
                self.assertEqual(leftovers, [], "有東西被 shell 執行了")
                self.assertEqual(r["argv"][2:4], ["--title", "Its 記帳  工具二"])

    def test_double_quotes_would_expand(self):
        # 對照：把原話放進雙引號（舊範本的寫法）會把 $AAPL 吃掉
        r, _ = self.run_bash(f'cchub new x --title "{self.SAMPLE.replace(chr(96), "").replace(chr(34), "")}"')
        self.assertNotIn("$AAPL", r["argv"][-1])

    def test_brief_stdin_reaches_claude_md_verbatim(self):
        h = helpers.TempHome()
        try:
            h.write(".config/systemd/user/cchub-rc@.service", "[Service]\n")
            sysd = helpers.FakeSystemd(on_start=helpers.serve_simulator(h))
            ctx = helpers.make_ctx(h, sysd, stdin=io.StringIO(self.SAMPLE + "\n"))
            self.assertEqual(main(["new", "ledger", "--title", "記帳 'x' `id`\n工具", "--brief-stdin"], ctx=ctx), 0,
                             ctx.err.getvalue())
            with open(os.path.join(h.project, "ledger", "CLAUDE.md"), encoding="utf-8") as f:
                md = f.read()
            self.assertIn(self.SAMPLE, md)
            self.assertTrue(md.startswith("# 記帳 x id 工具\n"))
        finally:
            h.cleanup()


class D6NameNewlineTest(unittest.TestCase):
    """D6【低】：new 名稱尾端換行（exp_new_output：new 'nl\\n' rc=0 並建出 'nl\\n'）。"""

    def test_new_trailing_newline_rejected(self):
        h = helpers.TempHome()
        try:
            h.write(".config/systemd/user/cchub-rc@.service", "[Service]\n")
            before = h.read_claude_json()
            ctx = helpers.make_ctx(h, helpers.FakeSystemd(on_start=helpers.serve_simulator(h)))
            self.assertEqual(main(["new", "nl\n"], ctx=ctx), 1)
            self.assertEqual([n for n in os.listdir(h.project) if n.startswith("nl")], [])
            self.assertEqual(h.read_claude_json(), before)
        finally:
            h.cleanup()
        for n in ("ledger\n", "ledger\r", "ledger\t", "ledger\u3000"):
            with self.subTest(n=repr(n)), self.assertRaises(CchubError):
                validate_new_name(n)


class InstallBase(unittest.TestCase):
    SETTINGS = {"permissions": {"allow": ["Bash(ls *)"]}, "theme": "dark"}

    def setUp(self):
        self.h = helpers.TempHome()
        self.settings = self.h.write(".claude/settings.json",
                                     json.dumps(self.SETTINGS, indent=2, ensure_ascii=False) + "\n")
        helpers.install_fake_claude(self.h)

    def tearDown(self):
        self.h.cleanup()

    def ctx(self, **kw):
        return helpers.make_ctx(self.h, isatty=lambda: True, ask=lambda p: "yes", **kw)

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()


class D7UninstallRulesTest(InstallBase):
    """D7【低】：uninstall 不能刪到使用者自己原有的規則；registry 不見就什麼都不刪。"""

    def test_I2_preexisting_rules_survive_uninstall(self):
        pre = dict(self.SETTINGS, permissions={"ask": ASK_RULES + ["Bash(rm -rf *)"], "allow": ["Bash(ls *)"]})
        with open(self.settings, "w") as f:
            f.write(json.dumps(pre, indent=2, ensure_ascii=False) + "\n")
        before = self.read(self.settings)
        self.assertEqual(main(["install"], ctx=self.ctx()), 0)
        self.assertEqual(main(["uninstall"], ctx=self.ctx()), 0)
        self.assertEqual(self.read(self.settings), before)

    def test_I5_registry_missing(self):
        self.assertEqual(main(["install"], ctx=self.ctx()), 0)
        installed = self.read(self.settings)
        os.unlink(self.h.paths.registry_file)
        self.assertEqual(main(["uninstall"], ctx=self.ctx()), 0)
        self.assertEqual(self.read(self.settings), installed)


class D8ExistingFilesTest(InstallBase):
    """D8【低】：既有的 skill 被覆蓋前要備份並警告。"""

    def test_I4_existing_skill(self):
        p = self.h.paths
        os.makedirs(p.skill_dir)
        with open(p.skill_file, "w") as f:
            f.write("USER OWN SKILL")
        c = self.ctx()
        main(["install", "--dry-run"], ctx=c)
        self.assertIn("已存在而且內容不同", c.out.getvalue())
        self.assertEqual(main(["install"], ctx=self.ctx()), 0)
        backups = [n for n in os.listdir(p.backups_dir) if "SKILL" in n]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(p.backups_dir, backups[0])) as f:
            self.assertEqual(f.read(), "USER OWN SKILL")
        c = self.ctx()                                            # uninstall 之後原檔的去向：備份還在、有告訴使用者
        self.assertEqual(main(["uninstall"], ctx=c), 0, c.err.getvalue())
        self.assertFalse(os.path.exists(p.skill_file))           # cchub 版（hash 相符）被刪
        with open(os.path.join(p.backups_dir, backups[0])) as f:
            self.assertEqual(f.read(), "USER OWN SKILL")
        self.assertIn(backups[0], c.out.getvalue())


class D9NoEnvOverrideTest(unittest.TestCase):
    """D9【低】：正式程式不讀 CCHUB_HOME 之類的路徑覆寫環境變數。"""

    def test_paths_ignore_cchub_home(self):
        fake = "/tmp/cchub-should-not-be-used"
        if hasattr(Paths, "default"):
            old = os.environ.get("CCHUB_HOME")
            os.environ["CCHUB_HOME"] = fake
            try:
                home = Paths.default().home
            finally:
                if old is None:
                    del os.environ["CCHUB_HOME"]
                else:
                    os.environ["CCHUB_HOME"] = old
        else:
            home = Paths.from_env({"CCHUB_HOME": fake, "HOME": fake}).home
        self.assertEqual(home, pwd.getpwuid(os.getuid()).pw_dir)

    def test_production_code_reads_no_path_env(self):
        src = os.path.join(helpers.REPO, "cchub")
        for name in os.listdir(src):
            if name.endswith(".py"):
                with open(os.path.join(src, name), encoding="utf-8") as f:
                    code = f.read()
                self.assertNotIn('"CCHUB_HOME"', code, name)
                self.assertNotIn("environ.get(\"HOME\")", code, name)


class D10ServeRevalidationTest(ServeBase):
    """D10【低】：exp_paths P4——實例名與資料夾不對應、資料夾是繼承信任且有 hooks → 不能啟動 claude。"""

    def test_P4(self):
        risky = self.h.mkdir("work", "projects", "risky")
        self.h.write("work/projects/risky/.claude/settings.json", '{"hooks": {"Stop": [1]}}')
        rec = os.path.join(self.h.tmp, "rec.json")
        helpers.install_fake_claude(self.h, output="·✔︎· Ready · risky · main\n", record=rec)
        for inst in ("totally-unrelated-name", instance_for_dir(risky)):
            with self.subTest(inst=inst):
                write_instance_config(self.h.paths, inst, {"dir": risky, "title": "x", "mode": "auto", "capacity": 3})
                rc, out, st = self.run_sup(inst)
                self.assertEqual(rc, PERMANENT_EXIT)
                self.assertFalse(os.path.exists(rec), "claude 不應該被啟動")


class D11ClaudeJsonEdgeTest(unittest.TestCase):
    """D11【低】：孤立 surrogate → 可讀的錯誤、不 traceback、不寫不備份。"""

    def test_T4_lone_surrogate(self):
        h = helpers.TempHome()
        try:
            with open(h.paths.claude_json, "w") as f:
                f.write('{\n  "x": "\\ud83d",\n  "projects": {}\n}')
            with open(h.paths.claude_json, "rb") as f:
                before = f.read()
            h.write(".config/systemd/user/cchub-rc@.service", "[Service]\n")
            ctx = helpers.make_ctx(h, helpers.FakeSystemd(on_start=helpers.serve_simulator(h)))
            rc = main(["new", "surr"], ctx=ctx)                    # 不能丟出未處理的例外
            self.assertEqual(rc, 1)
            self.assertIn("無法重新序列化", ctx.err.getvalue())
            with open(h.paths.claude_json, "rb") as f:
                self.assertEqual(f.read(), before)
            self.assertEqual([n for n in os.listdir(h.paths.backups_dir) if n.startswith("cchub-")], [])
            self.assertFalse(os.path.exists(h.paths.claude_json + ".lock"))
        finally:
            h.cleanup()


class D12SkillRestartWordingTest(unittest.TestCase):
    """D12【低】：延後 15 秒只在手機（cchub 單元內）成立；在電腦上會立刻重啟。"""

    def test_wording(self):
        with open(SKILL, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("在手機的 session 裡", text)
        self.assertIn("在電腦上執行則會**立刻**重啟", text)


if __name__ == "__main__":
    unittest.main()
