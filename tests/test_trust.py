"""F6 信任判定、§5.1 規則 5 open 政策（AC12）、§5.2 信任寫入（AC11）。"""

import json
import os
import stat
import subprocess
import time
import unittest

import helpers
from cchub import trust
from cchub.trust import (BACKUP_PREFIX, CLI_PROJECT_DEFAULTS, ClaudeJsonLock, TrustRefused, canonical_repo_root,
                         check_open_policy, find_git_root, grant_trust_for_new_project, revoke_trust_keys,
                         rotate_backups, trust_info)
from cchub.units import read_registry, write_registry
from cchub.util import CchubError

GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT_ENV.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True)


class TrustAlgorithmTest(unittest.TestCase):
    """F6：往上找、遇到 git 根就停、家目錄不算；worktree 用 canonical repo 根。"""

    def setUp(self):
        self.h = helpers.TempHome()
        self.plain = self.h.mkdir("work", "projects", "plain")
        self.repo = self.h.mkdir("work", "projects", "repo")
        git("init", "-q", self.repo)
        self.sub = self.h.mkdir("work", "projects", "repo", "sub")

    def tearDown(self):
        self.h.cleanup()

    def info(self, p, cfg=None):
        return trust_info(p, cfg or self.h.read_claude_json(), self.h.home)

    def test_non_git_inherits(self):
        i = self.info(self.plain)
        self.assertTrue(i.trusted)
        self.assertFalse(i.own)
        self.assertEqual(i.key, self.h.project)

    def test_git_root_stops_walk(self):
        self.assertFalse(self.info(self.repo).trusted)
        self.assertFalse(self.info(self.sub).trusted)

    def test_own_record(self):
        cfg = self.h.read_claude_json()
        cfg["projects"][self.repo] = {"hasTrustDialogAccepted": True}
        i = self.info(self.repo, cfg)
        self.assertTrue(i.trusted and i.own)
        j = self.info(self.sub, cfg)
        self.assertTrue(j.trusted and j.inherited)

    def test_home_does_not_count(self):
        cfg = {"projects": {self.h.home: {"hasTrustDialogAccepted": True}}}
        self.assertFalse(self.info(self.plain, cfg).trusted)

    def test_not_true_is_untrusted(self):
        cfg = {"projects": {self.h.project: {"hasTrustDialogAccepted": "yes"}}}
        self.assertFalse(self.info(self.plain, cfg).trusted)

    def test_worktree_uses_canonical_root(self):
        main = self.h.mkdir("work", "projects", "mainrepo")
        git("init", "-q", main)
        with open(os.path.join(main, "a.txt"), "w") as f:
            f.write("x")
        git("add", "a.txt", cwd=main)
        git("commit", "-q", "-m", "init", cwd=main)
        wt = os.path.join(self.h.project, "wt")
        git("worktree", "add", "-q", wt, cwd=main)
        self.assertEqual(find_git_root(wt), wt)
        self.assertEqual(canonical_repo_root(wt), main)
        cfg = self.h.read_claude_json()
        cfg["projects"][main] = {"hasTrustDialogAccepted": True}
        i = self.info(wt, cfg)
        self.assertTrue(i.trusted)
        self.assertEqual(i.key, main)
        self.assertFalse(i.own)

    def test_bogus_git_file(self):
        d = self.h.mkdir("work", "projects", "bogus")
        with open(os.path.join(d, ".git"), "w") as f:
            f.write("gitdir: /nonexistent/.git/worktrees/x\n")
        self.assertEqual(canonical_repo_root(d), d)
        self.assertFalse(self.info(d).trusted)


class OpenPolicyTest(unittest.TestCase):
    """AC12：繼承信任且含 .mcp.json／hooks 的拒絕；有自身信任紀錄的通過。"""

    def setUp(self):
        self.h = helpers.TempHome()

    def tearDown(self):
        self.h.cleanup()

    def check(self, p, cfg=None):
        return check_open_policy(p, cfg or self.h.read_claude_json(), self.h.home)

    def test_inherited_with_mcp_refused(self):
        d = self.h.mkdir("work", "projects", "mcp")
        self.h.write("work/projects/mcp/.mcp.json", '{"mcpServers": {}}')
        with self.assertRaises(CchubError) as cm:
            self.check(d)
        self.assertIn(".mcp.json", str(cm.exception))
        self.assertIn("回電腦", str(cm.exception))

    def test_own_record_with_mcp_passes(self):
        d = self.h.mkdir("work", "projects", "mcp")
        self.h.write("work/projects/mcp/.mcp.json", '{"mcpServers": {}}')
        cfg = self.h.read_claude_json()
        cfg["projects"][d] = {"hasTrustDialogAccepted": True}
        self.assertTrue(self.check(d, cfg).own)

    def test_inherited_settings(self):
        cases = {
            "hooks": ('{"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "x"}]}]}}', True),
            "perm": ('{"permissions": {"allow": ["Bash(*)"]}}', True),
            "local": (None, True),
            "env": ('{"env": {"NODE_OPTIONS": "--require /tmp/x.js"}}', True),
            "broken": ("{not json", True),
            "harmless": ('{"model": "sonnet", "permissions": {}}', False),
        }
        for name, (text, refused) in cases.items():
            with self.subTest(name=name):
                d = self.h.mkdir("work", "projects", name)
                if name == "local":
                    self.h.write(f"work/projects/{name}/.claude/settings.local.json", '{"hooks": {"Stop": [1]}}')
                else:
                    self.h.write(f"work/projects/{name}/.claude/settings.json", text)
                if refused:
                    with self.assertRaises(CchubError):
                        self.check(d)
                else:
                    self.assertTrue(self.check(d).inherited)

    def test_inherited_clean_passes(self):
        d = self.h.mkdir("work", "projects", "clean")
        self.h.write("work/projects/clean/README.md", "hi")
        self.assertTrue(self.check(d).trusted)

    def test_untrusted_refused(self):
        d = self.h.mkdir("work", "projects", "repo")
        git("init", "-q", d)
        with self.assertRaises(CchubError) as cm:
            self.check(d)
        self.assertIn("還沒受信任", str(cm.exception))


class TrustWriteTest(unittest.TestCase):
    """AC11：信任只寫給同一次呼叫剛建立、只有模板的資料夾；除目標鍵外完全相同；備份只有 cchub- 前綴。"""

    def setUp(self):
        self.h = helpers.TempHome()
        self.p = self.h.paths
        self.dir = os.path.join(self.h.project, "ledger")
        os.mkdir(self.dir)
        with open(os.path.join(self.dir, "CLAUDE.md"), "w") as f:
            f.write("# ledger\n")
        with open(os.path.join(self.dir, ".gitignore"), "w") as f:
            f.write(".env\n")
        git("init", "-q", self.dir)
        self.token = "tok123"
        self.fd = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.addCleanup(os.close, self.fd)
        st = os.fstat(self.fd)
        reg = read_registry(self.p)
        reg["pending_new"][self.token] = {"dir": self.dir, "created_at": time.time(), "ino": st.st_ino, "dev": st.st_dev}
        write_registry(self.p, reg)
        self.user_backup = os.path.join(self.p.backups_dir, "claude.json.20260918-203358.bak")
        with open(self.user_backup, "w") as f:
            f.write("user's own backup")
        with open(self.p.claude_json, "rb") as f:
            self.orig_bytes = f.read()
        self.logs = []
        self.sleeps = []

    def tearDown(self):
        self.h.cleanup()

    def grant(self, token=None, **kw):
        kw.setdefault("sleep", self.sleeps.append)
        kw.setdefault("log", self.logs.append)
        return grant_trust_for_new_project(self.p, self.fd, token or self.token, self.dir, self.h.project, **kw)

    def cchub_backups(self):
        return sorted(n for n in os.listdir(self.p.backups_dir) if n.startswith(BACKUP_PREFIX))

    def assert_untouched(self):
        with open(self.p.claude_json, "rb") as f:
            self.assertEqual(f.read(), self.orig_bytes)
        self.assertEqual(self.cchub_backups(), [])
        self.assertFalse(os.path.exists(self.p.claude_json + ".lock"))

    def test_success_only_target_key_changes(self):
        before = self.h.read_claude_json()
        key = self.grant()
        self.assertEqual(key, self.dir)
        after = self.h.read_claude_json()
        want = dict(CLI_PROJECT_DEFAULTS, hasTrustDialogAccepted=True)
        self.assertEqual(after["projects"].pop(key), want)
        self.assertEqual(after, before)                      # 其他鍵值原封不動（含中文）
        self.assertEqual(stat.S_IMODE(os.stat(self.p.claude_json).st_mode), 0o600)
        self.assertFalse(os.path.exists(self.p.claude_json + ".lock"))
        backups = self.cchub_backups()
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.p.backups_dir, backups[0]), "rb") as f:
            self.assertEqual(f.read(), self.orig_bytes)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.p.backups_dir, backups[0])).st_mode), 0o600)
        self.assertTrue(os.path.exists(self.user_backup))
        self.assertIn(trust.READBACK_DELAY, self.sleeps)     # 放鎖後 2 秒回讀

    def test_existing_entry_only_flag_changes(self):
        cfg = self.h.read_claude_json()
        cfg["projects"][self.dir] = {"allowedTools": ["x"], "hasTrustDialogAccepted": False, "lastSessionId": "s"}
        self.h.write_claude_json(cfg)
        self.grant()
        after = self.h.read_claude_json()
        self.assertEqual(after["projects"][self.dir], {"allowedTools": ["x"], "hasTrustDialogAccepted": True,
                                                       "lastSessionId": "s"})

    def test_extra_file_refused(self):
        with open(os.path.join(self.dir, "evil.sh"), "w") as f:
            f.write("x")
        with self.assertRaises(TrustRefused) as cm:
            self.grant()
        self.assertIn("evil.sh", str(cm.exception))
        self.assert_untouched()

    def test_extra_claude_dir_refused(self):
        os.mkdir(os.path.join(self.dir, ".claude"))
        with self.assertRaises(TrustRefused):
            self.grant()
        self.assert_untouched()

    def test_symlinked_template_refused(self):
        os.unlink(os.path.join(self.dir, "CLAUDE.md"))
        os.symlink("/etc/hostname", os.path.join(self.dir, "CLAUDE.md"))
        with self.assertRaises(TrustRefused):
            self.grant()
        self.assert_untouched()

    def test_pending_too_old_refused(self):
        reg = read_registry(self.p)
        reg["pending_new"][self.token]["created_at"] = time.time() - 61
        write_registry(self.p, reg)
        with self.assertRaises(TrustRefused) as cm:
            self.grant()
        self.assertIn("60", str(cm.exception))
        self.assert_untouched()

    def test_wrong_token_refused(self):
        with self.assertRaises(TrustRefused):
            self.grant(token="other-token")
        self.assert_untouched()

    def test_D3_swapped_for_symlink_refused(self):
        """D3：建好之後資料夾被換成指向既有資料夾的符號連結 → 信任鍵由 fd 決定，拒絕，受害資料夾不受信任。"""
        victim = self.h.mkdir("secrets")
        os.rename(self.dir, self.dir + ".moved")
        os.symlink(victim, self.dir)
        with self.assertRaises(TrustRefused) as cm:
            self.grant()
        self.assertIn("移動或換掉", str(cm.exception))
        self.assert_untouched()

    def test_D3_renamed_within_root_refused(self):
        os.rename(self.dir, os.path.join(self.h.project, "other-name"))
        with self.assertRaises(TrustRefused):
            self.grant()
        self.assert_untouched()

    def test_D3_swap_inside_lock_refused(self):
        """鎖內的 precheck 再驗一次：第一次驗證後才被換掉也擋得住。"""
        victim = self.h.mkdir("secrets")
        orig = trust.verify_new_project
        calls = {"n": 0}

        def racy(*a, **kw):
            calls["n"] += 1
            r = orig(*a, **kw)
            if calls["n"] == 1:                    # 第一次驗證通過之後，攻擊者立刻換掉
                os.rename(self.dir, self.dir + ".moved")
                os.symlink(victim, self.dir)
            return r
        trust.verify_new_project = racy
        try:
            with self.assertRaises(TrustRefused):
                self.grant()
        finally:
            trust.verify_new_project = orig
        self.assertIsNone(self.h.read_claude_json()["projects"].get(victim))
        self.assert_untouched()

    def test_D11_lone_surrogate_aborts_cleanly(self):
        """D11：~/.claude.json 有孤立 surrogate（JSON.stringify 會寫成 \\ud83d）→ 中止、可讀錯誤、不寫不備份。"""
        with open(self.p.claude_json, "w") as f:
            f.write('{\n  "x": "\\ud83d",\n  "projects": {}\n}')
        with open(self.p.claude_json, "rb") as f:
            self.orig_bytes = f.read()
        with self.assertRaises(CchubError) as cm:
            self.grant()
        self.assertIn("無法重新序列化", str(cm.exception))
        self.assert_untouched()

    def test_js_format_bytes_preserved(self):
        """內容保留：JS JSON.stringify(…, null, 2) 格式（無結尾換行、含中文）→ 除了新增的鍵，位元組完全一致。"""
        js = ('{\n  "numStartups": 7,\n  "oauthAccount": {\n    "emailAddress": "x@example.invalid"\n  },\n'
              '  "projects": {\n    "/p/a": {\n      "allowedTools": [],\n      "hasTrustDialogAccepted": true\n'
              '    }\n  },\n  "tipsHistory": {\n    "中文提示": 3\n  },\n  "emptyObj": {},\n  "emptyArr": [],\n'
              '  "flag": false,\n  "nothing": null\n}')
        with open(self.p.claude_json, "w", encoding="utf-8") as f:
            f.write(js)
        parsed = json.loads(js)
        self.assertEqual(json.dumps(parsed, indent=2, ensure_ascii=False), js)   # 夾具確實是 JS 格式
        key = self.grant()
        expected = json.loads(js)
        expected["projects"][key] = dict(CLI_PROJECT_DEFAULTS, hasTrustDialogAccepted=True)
        with open(self.p.claude_json, encoding="utf-8") as f:
            got = f.read()
        self.assertEqual(got, json.dumps(expected, indent=2, ensure_ascii=False))
        self.assertTrue(got.startswith(js[:js.index('"/p/a"')]))                # 目標鍵之前的位元組一字不差

    def test_float_format_change_is_semantic_only(self):
        """已知且接受：JS 的 0.00004 會被寫成 4e-05（數值相同），見 CLAUDE.md。"""
        with open(self.p.claude_json, "w") as f:
            f.write('{\n  "projects": {},\n  "a": 0.00004\n}')
        self.grant()
        with open(self.p.claude_json) as f:
            text = f.read()
        self.assertIn('"a": 4e-05', text)
        self.assertEqual(json.loads(text)["a"], 0.00004)

    def test_replaced_dir_refused(self):
        reg = read_registry(self.p)
        reg["pending_new"][self.token]["ino"] = 1
        write_registry(self.p, reg)
        with self.assertRaises(TrustRefused):
            self.grant()
        self.assert_untouched()

    def test_parse_failure_aborts(self):
        with open(self.p.claude_json, "w") as f:
            f.write("{broken")
        with open(self.p.claude_json, "rb") as f:
            self.orig_bytes = f.read()
        with self.assertRaises(CchubError) as cm:
            self.grant()
        self.assertIn("解析失敗", str(cm.exception))
        self.assert_untouched()

    def test_rotation_keeps_10_and_only_cchub_prefix(self):
        for i in range(12):
            with open(os.path.join(self.p.backups_dir, f"{BACKUP_PREFIX}20200101-0000{i:02d}-000000"), "w") as f:
                f.write("old")
        other = os.path.join(self.p.backups_dir, "claude.json.backup.20260913")
        with open(other, "w") as f:
            f.write("keep")
        self.grant()
        backups = self.cchub_backups()
        self.assertEqual(len(backups), 10)
        self.assertNotIn(f"{BACKUP_PREFIX}20200101-000000-000000", backups)
        self.assertTrue(backups[-1] > f"{BACKUP_PREFIX}2026")   # 新的那份留著
        self.assertTrue(os.path.exists(self.user_backup))
        self.assertTrue(os.path.exists(other))

    def test_rotate_ignores_directories(self):
        os.mkdir(os.path.join(self.p.backups_dir, BACKUP_PREFIX + "dir"))
        self.assertEqual(rotate_backups(self.p.backups_dir, keep=0), [])

    def test_stale_lock_removed(self):
        lock = self.p.claude_json + ".lock"
        os.mkdir(lock)
        old = time.time() - 120
        os.utime(lock, (old, old))
        self.grant()
        self.assertFalse(os.path.exists(lock))
        self.assertTrue(any("殘留" in m for m in self.logs))
        self.assertTrue(self.h.read_claude_json()["projects"][self.dir]["hasTrustDialogAccepted"])

    def test_fresh_lock_times_out(self):
        lock = self.p.claude_json + ".lock"
        os.mkdir(lock)
        clock = helpers.FakeClock(start=0.0)
        with self.assertRaises(CchubError) as cm:
            self.grant(clock=clock, sleep=clock.sleep)
        self.assertIn("取不到", str(cm.exception))
        self.assertGreaterEqual(clock.t, trust.LOCK_TIMEOUT)
        self.assertTrue(os.path.isdir(lock))          # 別人的鎖不能動
        os.rmdir(lock)
        with open(self.p.claude_json, "rb") as f:
            self.assertEqual(f.read(), self.orig_bytes)

    def test_lock_waits_then_acquires(self):
        lock = self.p.claude_json + ".lock"
        os.mkdir(lock)
        calls = []

        def sleep(s):
            calls.append(s)
            if len(calls) == 3:
                os.rmdir(lock)     # 模擬 CLI 放鎖
        with ClaudeJsonLock(self.p.claude_json, sleep=sleep):
            self.assertTrue(os.path.isdir(lock))
        self.assertEqual(calls, [trust.LOCK_RETRY_INTERVAL] * 3)
        self.assertFalse(os.path.exists(lock))

    def test_readback_retry(self):
        state = {"n": 0}

        def sleep(s):
            if s == trust.READBACK_DELAY:
                state["n"] += 1
                if state["n"] == 1:     # 模擬 CLI 用舊內容覆寫回去
                    with open(self.p.claude_json, "wb") as f:
                        f.write(self.orig_bytes)
        self.grant(sleep=sleep)
        self.assertEqual(state["n"], 2)
        self.assertTrue(self.h.read_claude_json()["projects"][self.dir]["hasTrustDialogAccepted"])
        self.assertEqual(len(self.cchub_backups()), 2)

    def test_readback_gives_up_after_3(self):
        def sleep(s):
            if s == trust.READBACK_DELAY:
                with open(self.p.claude_json, "wb") as f:
                    f.write(self.orig_bytes)
        with self.assertRaises(CchubError) as cm:
            self.grant(sleep=sleep)
        self.assertIn("回讀失敗", str(cm.exception))

    def test_no_function_trusts_existing_folders(self):
        # 設計約束：模組裡唯一寫 hasTrustDialogAccepted=True 的公開入口是 grant_trust_for_new_project
        public = [n for n in dir(trust) if not n.startswith("_") and "trust" in n.lower() and callable(getattr(trust, n))]
        writers = [n for n in public if n.startswith(("grant", "set", "add", "write"))]
        self.assertEqual(writers, ["grant_trust_for_new_project"])

    def test_revoke(self):
        key = self.grant()
        before = self.h.read_claude_json()
        changed = revoke_trust_keys(self.p, [key, "/not/there"], sleep=lambda s: None)
        self.assertEqual(changed, [key])
        after = self.h.read_claude_json()
        self.assertFalse(after["projects"][key]["hasTrustDialogAccepted"])
        after["projects"][key]["hasTrustDialogAccepted"] = True
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
