"""_serve：stdout 固定文法、stderr 完全吻合＋30 秒窗、啟動前重驗實例設定、
退避（等網路與 401 的內部退避）、SIGTERM 轉發、env -i 的最小環境。

stdout／stderr 的格式依 CLI 實際的輸出重建（狀態顯示寫 stdout、致命錯誤寫 stderr）。
"""

import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest

import helpers
from cchub.names import instance_for_dir
from cchub.serve import (Backoff, DETAIL_MAX, PERMANENT_EXIT, StderrMonitor, StdoutFilter, Supervisor,
                         crash_backoff_delay, match_stderr, strip_ansi, wait_for_network)
from cchub.units import read_instance_state, write_instance_config, write_instance_state

ESC = "\x1b"


def osc8(url, text, st="\x07"):
    # CLI 的終端機超連結用 BEL 結尾；也測 ESC \ 結尾
    return f"{ESC}]8;;{url}{st}{text}{ESC}]8;;{st}"


def dim(s):
    return f"{ESC}[2m{s}{ESC}[22m"


SESSION_URL = "https://claude.ai/code/session_EXAMPLEaaaa1111?from=cli"      # 假值
SESSION_URL_CLEAN = "https://claude.ai/code/session_EXAMPLEaaaa1111"
ENV_URL = "https://claude.ai/code?environment=env_EXAMPLEbbbb2222"


def status_line(label, name, branch="HEAD"):
    tail = dim(" · ") + dim(name) + (dim(" · ") + dim(branch) if branch else "")
    return f"{ESC}[36m·✔︎·{ESC}[39m {ESC}[36m{label}{ESC}[39m{tail}\n"


def block_multi(name, title, summary, url=SESSION_URL):
    """capacity>1：狀態列＋縮排的 capacity 行＋縮排的 session 行（標題是 OSC 8 連結＋活動摘要）。"""
    return (f"{ESC}[6A{ESC}[J" + status_line("Connected", name)
            + f"    {dim('Capacity: 1/3 · New sessions will be created in the current directory')}\n"
            + f"    {osc8(url, title)}{dim(' ' + summary)}\n\n"
            + dim(f"Continue coding in the Claude mobile app or {osc8(ENV_URL, ENV_URL)}") + "\n"
            + dim("Or ask Claude to work in this folder from your projects on claude.ai") + "\n"
            + f"{ESC}[2m{ESC}[3mspace to show QR code{ESC}[23m{ESC}[22m\n")


def block_single(title, name="proj"):
    """capacity=1：CLI 會把狀態字換成 session 標題。"""
    return (f"{ESC}[36m·✔︎·{ESC}[39m {ESC}[36m{title}{ESC}[39m{dim(' · ')}{dim(name)}{dim(' · ')}{dim('main')}\n"
            f"    {dim('Capacity: 1/1 · New sessions will be created in the current directory')}\n")


# 實測格式：一次性同意沒答（stdout，提示沒有換行）
FIXTURE_DIALOG = (
    "\nTake this session with you and pick up right where you left off on any device.\n"
    "Open the Code tab in the Claude mobile app, visit claude.ai/code in a browser,\n"
    "or ask Claude to work in this folder from one of your projects on claude.ai.\n\n"
    "The session keeps running on this machine. Use your other devices as a remote\n"
    "control. Press Ctrl+C to stop.\n\n"
    "Enable Remote Control? (y/n) "
)

# 對抗性夾具：session 標題與活動摘要來自對話，可以是任何字樣
ADVERSARIAL_STDOUT = (
    block_multi("proj", "Error: Workspace not trusted fix", "Reading SECRET-PLAN-7731.md")
    + block_multi("proj", "Warning: must be logged in", "Running cat SECRET-PLAN-7731")
    + block_multi("proj", "see https://claude.ai/code/session_EVIL999", "x",
                  url="https://claude.ai/code/session_EVIL888")
    + block_single("Connected · https://claude.ai/code/session_EVIL123")
    + block_single("Ready")                                   # 標題剛好是固定字 → 與真狀態列同形，不洩漏內容
    + status_line("Connected", "not-this-folder", "main")     # 名稱不是這個資料夾
    + "[12:00:00] Error: Workspace not trusted SECRET-PLAN-7731\n"
    + "SECRET-PLAN-7731 plain text at column 0\n"
)
FORBIDDEN = ("SECRET-PLAN-7731", "EVIL", "Take this session", "Open the Code tab", "Press Ctrl+C",
             "Or ask Claude", "space to show QR code", "Continue coding", "?from=cli", "Workspace not trusted fix",
             "not-this-folder")

MSG_UNTRUSTED = ("Error: Workspace not trusted. Please run `claude` in {dir} first to review and accept "
                 "the workspace trust dialog.\n")
MSG_NOT_LOGGED_IN = ("Error: You must be logged in to use Remote Control.\n\nRemote Control is only available with "
                     "claude.ai subscriptions. Run `claude auth login` to sign in with your claude.ai account.\n")
MSG_409 = "Error: This folder is already served by another Claude Code on this device. Stop it first.\n"
MSG_401 = ("Error: Registration: Authentication failed (401): token expired. Remote Control is only available with "
           "claude.ai subscriptions. Please use `/login` to sign in with your claude.ai account.\n")
MSG_CUSTOM_409 = "Error: Environment already registered for this directory (409)."
EXITING = "Exiting in about 60 seconds.\n"


def feed_all(f, text):
    out = []
    for line in text.split("\n"):
        out.extend(f.feed(line))
    return out


class StdoutFilterTest(unittest.TestCase):
    def test_connected_fixture(self):
        f = StdoutFilter({"myproj"})
        out = feed_all(f, block_multi("myproj", "demo-session-title", "idle"))
        self.assertEqual(out, ["[狀態] Connected · myproj · HEAD", f"[網址] environment {ENV_URL}"])
        self.assertEqual(f.status, "ready")
        self.assertEqual(f.env_url, ENV_URL)
        self.assertEqual(f.session_urls, [])            # 狀態列之後的 session 網址（標題連結）一律不收

    def test_idle_banner_and_st_terminator(self):
        f = StdoutFilter({"proj"})
        out = feed_all(f, status_line("Ready", "proj", None)
                       + dim(f"Code anywhere with the Claude mobile app or {osc8(ENV_URL, ENV_URL, ESC + chr(92))}") + "\n")
        self.assertEqual(out, ["[狀態] Ready · proj", f"[網址] environment {ENV_URL}"])

    def test_adversarial_titles_do_not_leak(self):
        """標題／活動摘要（對話衍生）不得進 journal；注入的 session 網址不得被收進去。"""
        f = StdoutFilter({"proj"})
        out = feed_all(f, ADVERSARIAL_STDOUT)
        joined = "\n".join(out)
        for bad in FORBIDDEN:
            self.assertNotIn(bad, joined)
        self.assertEqual(f.session_urls, [])
        # 最後一個被接受的是「標題剛好是 Ready」的 capacity=1 狀態列：與真狀態列同形，不含對話內容
        self.assertEqual(f.status_line, "Ready · proj · main")

    def test_session_url_only_before_first_status(self):
        f = StdoutFilter({"proj"})
        out = f.feed(f"{osc8(SESSION_URL, 'x')}")               # 狀態列之前、第 0 欄
        self.assertEqual(out, [f"[網址] session {SESSION_URL_CLEAN}"])
        f.feed(status_line("Connecting", "proj").rstrip("\n"))
        self.assertEqual(f.feed(osc8("https://claude.ai/code/session_LATE", "y")), [])
        self.assertEqual(f.session_urls, [SESSION_URL_CLEAN])

    def test_status_dedup_spinner_reconnecting(self):
        f = StdoutFilter({"proj"})
        out = []
        for fr in ["·|·", "·/·", "·—·", "·\\·"] * 5:
            out += f.feed(f"{fr} Connecting · proj · main")
        out += f.feed("·|· Connecting")                         # 名稱還沒設定時
        self.assertEqual(out, ["[狀態] Connecting · proj · main", "[狀態] Connecting"])
        out = []
        for i in range(10):
            out += f.feed(f"·|· Reconnecting · retrying in {10 - i}.0s · disconnected {i}s")
        self.assertEqual(out, ["[狀態] Reconnecting"])
        self.assertEqual(f.status, "reconnecting")
        out = feed_all(f, block_multi("proj", "t", "s") * 4)
        self.assertEqual(sum(1 for x in out if x.startswith("[狀態]")), 1)

    def test_name_must_match(self):
        f = StdoutFilter({"proj"})
        self.assertEqual(f.feed(status_line("Connected", "other").rstrip("\n")), [])
        self.assertIsNone(f.status)
        self.assertEqual(f.feed(status_line("Ready", "proj", None).rstrip("\n")), ["[狀態] Ready · proj"])

    def test_strip_ansi(self):
        self.assertEqual(strip_ansi(f"{ESC}[31mred{ESC}[0m {osc8('https://x', 'link')}"), "red link")
        self.assertEqual(strip_ansi("progress 10%\rprogress 100%"), "progress 100%")
        self.assertEqual(strip_ansi("a\x07b\x00c"), "abc")


class StderrMonitorTest(unittest.TestCase):
    D = "/home/alice/work/projects/proj"

    def kinds(self, text, d=D):
        clock = helpers.FakeClock(0.0)
        m = StderrMonitor(d, clock)
        for line in text.split("\n"):
            m.feed(line)
        return m

    def test_exact_messages(self):
        self.assertEqual(self.kinds(MSG_UNTRUSTED.format(dir=self.D)).classify(1)[0], "untrusted")
        self.assertEqual(self.kinds(MSG_NOT_LOGGED_IN).classify(1)[0], "auth")

    def test_409_is_never_permanent(self):
        m = self.kinds(MSG_409 + EXITING)
        self.assertEqual(m.last_known_error(), "already_served")
        self.assertIsNone(m.classify(1))                         # 窗口內也不是永久性
        self.assertEqual(m.detail, MSG_409.strip())               # 預設原文仍記 already_served，detail 是那一行

    def test_registration_failure_keeps_only_that_line(self):
        clock = helpers.FakeClock(0.0)
        m = StderrMonitor(self.D, clock)
        out = []
        for line in ("SECRET-STDERR-4455 random output", MSG_CUSTOM_409, EXITING.strip()):
            out += m.feed(line)
        self.assertEqual(m.last_known_error(), "registration_failed")
        self.assertEqual(m.detail, MSG_CUSTOM_409)
        self.assertIsNone(m.classify(1))
        self.assertEqual(m.unknown, 1)                            # 只有 SECRET 那行算未知
        self.assertIn(f"[錯誤] 註冊失敗：{MSG_CUSTOM_409}", out)
        self.assertFalse(any("SECRET" in x for x in out))

    def test_registration_lookback_is_5_lines(self):
        for gap, recognized in ((4, True), (5, False)):
            with self.subTest(gap=gap):
                m = StderrMonitor(self.D, helpers.FakeClock(0.0))
                m.feed("Error: something broke")
                for i in range(gap):
                    m.feed(f"filler {i}")
                m.feed(EXITING)
                self.assertEqual(m.last_known_error() == "registration_failed", recognized)

    def test_detail_sanitized_and_truncated(self):
        m = StderrMonitor(self.D, helpers.FakeClock(0.0))
        m.feed("Error: " + "\u200bX\u202eY" * 150 + "\x07")          # 去掉格式字元後仍超過 200 字
        m.feed(EXITING)
        self.assertEqual(len(m.detail), DETAIL_MAX)
        self.assertFalse(any(ch in m.detail for ch in "\u200b\u202e\x07"))

    def test_near_misses_are_not_permanent(self):
        """只認完全吻合；寧可漏判也不要誤判。"""
        cases = [
            MSG_UNTRUSTED.format(dir="/home/alice/work/projects/other"),                   # 不是這個資料夾
            "Error: Registration: folder is already being served elsewhere (409)",   # 伺服器自訂訊息（漏判可接受）
            "[10:00:00] Error: Session spawn failed: EEXIST: file already exists, open '/p/server.log'",  # 一般錯誤，剛好含 already
            "Error: port already in use by dev server",                              # 同上
            "Error: Workspace not trusted fix",                                      # 標題式的前綴
            "  " + MSG_409.strip(),                                                  # 縮排
            MSG_409.strip() + " extra",
            MSG_401,                                                                 # 401 不列為永久性
        ]
        for c in cases:
            with self.subTest(c=c[:50]):
                self.assertIsNone(self.kinds(c).classify(1))

    def test_401_is_known_but_transient(self):
        m = self.kinds(MSG_401)
        self.assertEqual(m.last_known_error(), "auth_401")
        self.assertIsNone(m.classify(1))
        self.assertEqual(match_stderr(MSG_401.strip(), self.D)[0], "auth_401")

    def test_window_30_seconds(self):
        """很久以前的訊息不算（必須在結束前 30 秒內）。"""
        clock = helpers.FakeClock(0.0)
        m = StderrMonitor(self.D, clock)
        for line in MSG_NOT_LOGGED_IN.split("\n"):
            m.feed(line)
        self.assertEqual(m.classify(clock.t + 30)[0], "auth")
        self.assertIsNone(m.classify(clock.t + 30.5))

    def test_unknown_lines_counted_not_kept(self):
        m = self.kinds("some SECRET text\nanother\n" + MSG_NOT_LOGGED_IN)
        self.assertEqual(m.unknown, 2)
        self.assertEqual([k for _, k in m.events], ["auth", "auth_hint"])


class BackoffTest(unittest.TestCase):
    def test_growth_cap_reset(self):
        b = Backoff()
        self.assertEqual([b.next_delay() for _ in range(8)], [10, 20, 40, 80, 160, 300, 300, 300])
        b.reset()
        self.assertEqual((b.current, b.failures), (10, 0))

    def test_wait_for_network(self):
        results = iter([False, False, False, True])
        sleeps = []
        b = Backoff()
        self.assertEqual(wait_for_network(lambda: next(results), sleeps.append, b), 3)
        self.assertEqual(sleeps, [10, 20, 40])
        self.assertEqual(b.current, 10)

    def test_crash_backoff_delay(self):
        self.assertEqual([crash_backoff_delay(n) for n in range(0, 8)], [0, 10, 20, 40, 80, 160, 300, 300])


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.h = helpers.TempHome()
        self.dir = self.h.mkdir("work", "projects", "proj")
        self.inst = instance_for_dir(self.dir)
        write_instance_config(self.h.paths, self.inst, {"dir": self.dir, "title": "proj", "mode": "auto",
                                                        "capacity": 3, "entry": False})
        self.rec = os.path.join(self.h.tmp, "record.json")
        self.environ = {"HOME": self.h.home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                        "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "ANTHROPIC_API_KEY": "fake",
                        "USE_LOCAL_OAUTH": "1", "USE_STAGING_OAUTH": "1"}

    def tearDown(self):
        self.h.cleanup()

    def run_sup(self, probe=lambda: True, sleep=None, inst=None, **kw):
        out = io.StringIO()
        sup = Supervisor(self.h.paths, inst or self.inst, probe=probe, sleep=sleep or (lambda s: None), out=out,
                         environ=self.environ, install_signals=False, **kw)
        rc = sup.run()
        return rc, out.getvalue(), read_instance_state(self.h.paths, inst or self.inst), sup

    def assert_no_leak(self, out, st):
        dumped = json.dumps(st, ensure_ascii=False)
        for bad in FORBIDDEN:
            self.assertNotIn(bad, out)
            self.assertNotIn(bad, dumped)

    def test_connected_then_exit(self):
        exe = helpers.install_fake_claude(self.h, output=block_multi("proj", "demo-session-title", "idle") * 3,
                                          exit=1, record=self.rec)
        rc, out, st, _ = self.run_sup()
        self.assertEqual(rc, 1)
        self.assertEqual(out.count("[狀態] Connected · proj · HEAD"), 1)
        self.assertEqual(st["env_url"], ENV_URL)
        self.assertEqual((st["status"], st["exit_code"], st["exe"]), ("exited", 1, exe))
        self.assertEqual(st["fast_failures"], 0)                  # 有上線過 → 不算快速失敗
        self.assertNotIn("demo-session-title", out)
        with open(self.rec) as f:
            r = json.load(f)
        self.assertEqual(r["argv"][1:], ["remote-control", "--name", "proj", "--spawn", "same-dir",
                                         "--capacity", "3", "--permission-mode", "auto"])
        self.assertNotIn("--verbose", r["argv"])
        self.assertEqual(os.path.realpath(r["cwd"]), self.dir)
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_API_KEY", "USE_LOCAL_OAUTH", "USE_STAGING_OAUTH"):
            self.assertNotIn(k, r["env_keys"])
        for k in ("HOME", "PATH", "LANG"):
            self.assertIn(k, r["env_keys"])

    def test_adversarial_title_then_disconnect_is_not_permanent(self):
        """標題含「Error: Workspace not trusted」＋斷線 exit 1 → 不得 exit 3、不得洩漏。"""
        helpers.install_fake_claude(self.h, output=ADVERSARIAL_STDOUT * 3,
                                    stderr="Error: Remote Control disconnected: network unreachable\n", exit=1)
        rc, out, st, _ = self.run_sup()
        self.assertEqual(rc, 1)
        self.assertIsNone(st["error"])
        self.assert_no_leak(out, st)
        self.assertIn("stderr 另有 1 行不在白名單內", out)

    def test_permanent_errors_exit_3(self):
        cases = {"untrusted": MSG_UNTRUSTED.format(dir=self.dir), "auth": MSG_NOT_LOGGED_IN}
        for kind, err_text in cases.items():
            with self.subTest(kind=kind):
                helpers.install_fake_claude(self.h, stderr=err_text, exit=1)
                rc, out, st, _ = self.run_sup()
                self.assertEqual(rc, PERMANENT_EXIT)
                self.assertEqual((st["status"], st["error"]["kind"], st["error"]["permanent"]), ("failed", kind, True))
                self.assertIn("exit 3", out)

    def test_same_messages_on_stdout_are_ignored(self):
        """永久性判定只看 stderr；同樣的字出現在 stdout（例如對話內容）不算。"""
        helpers.install_fake_claude(self.h, output=MSG_UNTRUSTED.format(dir=self.dir) + MSG_409 + MSG_NOT_LOGGED_IN,
                                    exit=1)
        rc, out, st, _ = self.run_sup()
        self.assertEqual(rc, 1)
        self.assertIsNone(st["error"])

    def test_stale_error_outside_window_is_not_permanent(self):
        """完全吻合的「未登入」不在結束前 30 秒內 → 不算永久性（用縮短的窗重現）；窗內 → 3。"""
        helpers.install_fake_claude(self.h, stderr=MSG_NOT_LOGGED_IN, hang=1.0, exit=1)
        rc, _, st, _ = self.run_sup(perm_window=0.3)
        self.assertEqual(rc, 1)
        self.assertEqual(st["last_error"]["kind"], "auth")                # 還是會記下來給 ls 看
        rc, _, st, _ = self.run_sup()                                     # 對照：預設 30 秒窗內 → 3
        self.assertEqual(rc, PERMANENT_EXIT)

    def test_409_inside_window_is_not_permanent(self):
        """409 印完立刻結束（在 30 秒窗內）也不回 3，並記下 last_error。"""
        helpers.install_fake_claude(self.h, stderr=MSG_409 + "Exiting in about 5 seconds.\n", exit=1)
        rc, out, st, _ = self.run_sup()
        self.assertEqual(rc, 1)
        self.assertEqual((st["status"], st["error"]), ("exited", None))
        self.assertEqual(st["last_error"]["kind"], "already_served")
        self.assertEqual(st["last_error_detail"], MSG_409.strip())
        self.assertNotIn("exit 3", out)

    def test_server_custom_409_recorded(self):
        """伺服器自訂文字的註冊失敗 → registration_failed，只存那一行。"""
        helpers.install_fake_claude(self.h, stderr="SECRET-STDERR-4455\n" + MSG_CUSTOM_409 + "\n" + EXITING, exit=1)
        rc, out, st, _ = self.run_sup()
        self.assertEqual(rc, 1)
        self.assertEqual(st["last_error"]["kind"], "registration_failed")
        self.assertEqual(st["last_error_detail"], MSG_CUSTOM_409)
        self.assertEqual(st["last_error"]["message"], f"註冊失敗：{MSG_CUSTOM_409}")
        self.assertIn(f"[錯誤] 註冊失敗：{MSG_CUSTOM_409}", out)
        self.assertNotIn("SECRET-STDERR", out)
        self.assertNotIn("SECRET-STDERR", json.dumps(st, ensure_ascii=False))

    def test_last_error_is_per_run(self):
        helpers.install_fake_claude(self.h, stderr=MSG_CUSTOM_409 + "\n" + EXITING, exit=1)
        _, _, st, _ = self.run_sup()
        self.assertEqual(st["last_error"]["kind"], "registration_failed")
        helpers.install_fake_claude(self.h, stderr="Error: something unrelated\n", exit=1)
        _, _, st, _ = self.run_sup()                                     # 下一輪不相干的失敗不能顯示舊原因
        self.assertIsNone(st["last_error"])
        self.assertIsNone(st["last_error_detail"])

    def test_401_transient_with_crash_backoff(self):
        helpers.install_fake_claude(self.h, stderr=MSG_401, exit=1)
        rc, out, st, _ = self.run_sup()
        self.assertEqual(rc, 1)
        self.assertIsNone(st["error"])
        self.assertEqual(st["last_error"]["kind"], "auth_401")
        self.assertEqual(st["fast_failures"], 1)
        self.assertIn("驗證失敗（401）", out)
        sleeps = []
        rc, out, st, _ = self.run_sup(sleep=sleeps.append)               # 下一次啟動前先退避 10 秒
        self.assertEqual(sleeps[0], 10)
        self.assertEqual(st["fast_failures"], 2)
        sleeps.clear()
        helpers.install_fake_claude(self.h, output=status_line("Ready", "proj"), exit=1)
        rc, out, st, _ = self.run_sup(sleep=sleeps.append)
        self.assertEqual(sleeps[0], 20)
        self.assertEqual(st["fast_failures"], 0)                         # 上線過 → 歸零

    def test_consent_prompt_is_not_permanent(self):
        helpers.install_fake_claude(self.h, output=FIXTURE_DIALOG, hang=30, record=self.rec)
        t0 = time.monotonic()
        rc, out, st, _ = self.run_sup()
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(rc, 1)
        self.assertEqual((st["status"], st["error"]["kind"], st["error"]["permanent"]), ("needs_consent", "consent", False))
        self.assertIn("claude remote-control 並回答 y", st["error"]["message"])
        self.assertTrue(os.path.exists(self.rec + ".term"))
        for bad in ("Take this session", "Open the Code tab", "Press Ctrl+C"):
            self.assertNotIn(bad, out)

    def test_consent_precheck_does_not_start_claude(self):
        cfg = self.h.read_claude_json()
        cfg["remoteDialogSeen"] = False
        self.h.write_claude_json(cfg)
        helpers.install_fake_claude(self.h, output=status_line("Ready", "proj"), record=self.rec)
        rc, out, st, _ = self.run_sup()
        self.assertEqual(rc, 1)
        self.assertEqual(st["status"], "needs_consent")
        self.assertFalse(os.path.exists(self.rec))

    def test_child_exit_codes(self):
        helpers.install_fake_claude(self.h, output=status_line("Ready", "proj"), exit=3)
        self.assertEqual(self.run_sup()[0], 1)          # 子程序自己的 3 不能被當成永久性錯誤
        helpers.install_fake_claude(self.h, output=status_line("Ready", "proj"), exit=0)
        rc, _, st, _ = self.run_sup()
        self.assertEqual((rc, st["status"]), (0, "exited"))

    def test_instance_revalidation(self):
        """實例名≠escape(資料夾)、不在允許範圍、未受信任、繼承信任但有 hooks → exit 3，不啟動 claude。"""
        helpers.install_fake_claude(self.h, output=status_line("Ready", "proj"), record=self.rec)
        risky = self.h.mkdir("work", "projects", "risky")
        self.h.write("work/projects/risky/.claude/settings.json", '{"hooks": {"Stop": [1]}}')
        repo = self.h.mkdir("work", "projects", "repo")
        subprocess.run(["git", "init", "-q", repo], check=True)
        cases = [
            ("totally-unrelated-name", {"dir": self.dir, "mode": "auto", "capacity": 3}, "config"),   # 實例名與資料夾不對應
            (instance_for_dir(risky), {"dir": risky, "mode": "auto", "capacity": 3}, "policy"),
            (instance_for_dir(repo), {"dir": repo, "mode": "auto", "capacity": 3}, "untrusted"),
            (None, {"dir": self.dir, "mode": "bypassPermissions", "capacity": 3}, "config"),
            (None, {"dir": "/etc", "mode": "auto", "capacity": 3}, "config"),
            (None, {"dir": self.dir, "mode": "auto", "capacity": 999}, "config"),
            (None, {"dir": self.dir, "mode": "auto", "capacity": 3, "entry": True}, "config"),
        ]
        for inst, cfg, kind in cases:
            with self.subTest(inst=inst, kind=kind):
                inst = inst or self.inst
                write_instance_config(self.h.paths, inst, cfg)
                rc, out, st, _ = self.run_sup(inst=inst)
                self.assertEqual(rc, PERMANENT_EXIT)
                self.assertEqual(st["error"]["kind"], kind)
                self.assertFalse(os.path.exists(self.rec))

    def test_missing_instance_and_no_binary(self):
        rc, _, st, _ = self.run_sup(inst="home-nobody-nothing")
        self.assertEqual((rc, st["error"]["kind"]), (PERMANENT_EXIT, "config"))
        rc, _, st, _ = self.run_sup()
        self.assertEqual((rc, st["error"]["kind"]), (PERMANENT_EXIT, "no_binary"))

    def test_old_version_warning(self):
        helpers.install_fake_claude(self.h, version="2.1.280", source="desktop", output=status_line("Ready", "proj"))
        rc, out, st, _ = self.run_sup()
        self.assertIn("2.1.281", out)
        self.assertTrue(any("Artifact" in w for w in st["warnings"]))

    def test_network_backoff(self):
        """探測失敗時不啟動 claude、退避會成長；探測恢復後 1 次就啟動、退避歸零。"""
        helpers.install_fake_claude(self.h, output=status_line("Ready", "proj"), record=self.rec)
        probes = []
        answers = [False] * 7 + [True]

        def probe():
            probes.append(len(probes))
            return answers[len(probes) - 1]

        sleeps = []

        def sleep(s):
            self.assertFalse(os.path.exists(self.rec), "網路沒通之前不能啟動 claude")
            sleeps.append(s)

        spawns = []
        real_popen = subprocess.Popen

        def popen(*a, **kw):
            spawns.append(len(probes))
            return real_popen(*a, **kw)

        backoff = Backoff()
        rc, out, st, _ = self.run_sup(probe=probe, sleep=sleep, popen=popen, backoff=backoff)
        self.assertEqual(sleeps, [10, 20, 40, 80, 160, 300, 300])
        self.assertEqual(spawns, [8])
        self.assertEqual((backoff.current, backoff.failures), (10, 0))
        self.assertTrue(os.path.exists(self.rec))
        self.assertIn("退避歸零", out)
        self.assertEqual(rc, 0)

    def test_title_and_instance_arg_sanitized(self):
        import contextlib
        from cchub.serve import run_serve, sanitize_title
        self.assertEqual(sanitize_title("--verbose", "x"), "verbose")
        self.assertEqual(sanitize_title("a\nb  c", "x"), "a b c")
        self.assertEqual(sanitize_title("It's \"記帳\" `id` $(x)\t工具", "x"), "Its 記帳 id (x) 工具")
        self.assertEqual(len(sanitize_title("長" * 100, "x")), 60)
        self.assertEqual(sanitize_title("'\"`", "-fallback"), "fallback")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for bad in ("../../tmp/evil", ".hidden", "a/b", "", "x" * 300, "x\n"):
                self.assertEqual(run_serve(self.h.paths, bad), PERMANENT_EXIT)
        self.assertEqual(err.getvalue().count("實例名不合法"), 6)


class ServeProcessTest(unittest.TestCase):
    """真的在子程序裡跑 _serve（用參數注入暫存家目錄，不靠環境變數）。"""

    def setUp(self):
        self.h = helpers.TempHome()
        self.dir = self.h.mkdir("work", "projects", "proj")
        self.inst = instance_for_dir(self.dir)
        write_instance_config(self.h.paths, self.inst, {"dir": self.dir, "title": "proj", "mode": "auto",
                                                        "capacity": 3, "entry": False})
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.h.update_config(probe_host="127.0.0.1", probe_port=self.srv.getsockname()[1])
        self.rec = os.path.join(self.h.tmp, "record.json")
        self.wrapper = helpers.serve_wrapper(self.h)

    def tearDown(self):
        self.srv.close()
        self.h.cleanup()

    def wait_state(self, status, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if read_instance_state(self.h.paths, self.inst).get("status") == status:
                return
            time.sleep(0.1)
        self.fail(f"狀態一直沒變成 {status}")

    def test_sigterm_forwarded(self):
        helpers.install_fake_claude(self.h, output=status_line("Ready", "proj"), hang=60, record=self.rec)
        p = subprocess.Popen([sys.executable, self.wrapper, self.h.home, self.inst],
                             env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            self.wait_state("ready")
            p.send_signal(signal.SIGTERM)
            out, _ = p.communicate(timeout=25)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 0, out.decode())
        self.assertTrue(os.path.exists(self.rec + ".term"), "SIGTERM 沒有轉給 claude")
        self.assertEqual(read_instance_state(self.h.paths, self.inst)["status"], "stopped")
        self.assertIn("[狀態] Ready · proj · HEAD", out.decode())

    def test_env_i_minimal_environment(self):
        """`env -i`（連 HOME、PATH 都沒有）也能啟動：開機時 systemd 給單元的環境很精簡。"""
        helpers.install_fake_claude(self.h, output=status_line("Ready", "proj"), exit=0, record=self.rec)
        r = subprocess.run(["env", "-i", sys.executable, self.wrapper, self.h.home, self.inst],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[狀態] Ready · proj · HEAD", r.stdout)
        with open(self.rec) as f:
            self.assertEqual(os.path.realpath(json.load(f)["cwd"]), self.dir)

    def test_paths_default_ignores_env(self):
        """Paths.default() 不讀 CCHUB_HOME／HOME，`env -i` 下也拿得到家目錄。"""
        import pwd
        code = ("import sys; sys.dont_write_bytecode = True; sys.path.insert(0, %r); "
                "from cchub.paths import Paths; print(Paths.default().home)" % helpers.REPO)
        for env in ({"CCHUB_HOME": self.h.home, "HOME": self.h.home, "PATH": "/usr/bin:/bin"}, {}):
            r = subprocess.run(["env", "-i", *[f"{k}={v}" for k, v in env.items()], sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.stdout.strip(), pwd.getpwuid(os.getuid()).pw_dir, r.stderr)


if __name__ == "__main__":
    unittest.main()
