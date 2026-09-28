"""名稱驗證、systemd-escape、名稱解析、路徑安全。"""

import os
import shutil
import subprocess
import unittest

import helpers
from cchub.names import (instance_for_dir, resolve_target, systemd_escape_path, systemd_unescape_path,
                         unit_for_instance, validate_new_name)
from cchub.paths import load_config
from cchub.util import CchubError


class NewNameTest(unittest.TestCase):
    def test_valid(self):
        for n in ("ledger", "a", "my-tool-2", "0abc", "a" * 40, "x-"):
            self.assertEqual(validate_new_name(n), n)

    def test_invalid(self):
        for n in ("../x", "A", "Ledger", "-x", "", "a" * 41, "x_y", "x.y", "中文", "a b", "x/y", ".x"):
            with self.subTest(n=n), self.assertRaises(CchubError):
                validate_new_name(n)

    def test_trailing_newline_and_whitespace_rejected(self):
        """re.match＋$ 會讓尾端 \\n 過關；一律 fullmatch。"""
        for n in ("ledger\n", "ledger\r", "ledger\t", "ledger\r\n", "ledger\u3000", "ledger\u00a0",
                  "ledger ", " ledger", "\nledger", "led\nger", "ledger\u200b"):
            with self.subTest(n=repr(n)), self.assertRaises(CchubError):
                validate_new_name(n)

    def test_modes_exact_match(self):
        from cchub.paths import validate_mode
        for m in ("auto\n", " auto", "auto ", "Auto", "bypassPermissions\n"):
            with self.subTest(m=repr(m)), self.assertRaises(CchubError):
                validate_mode(m)

    def test_reserved(self):
        for n in ("entry", "hub", "cchub"):
            with self.subTest(n=n), self.assertRaises(CchubError) as cm:
                validate_new_name(n)
            self.assertIn("保留字", str(cm.exception))


class EscapeTest(unittest.TestCase):
    CASES = {
        "/home/alice/work/projects": "home-alice-work-projects",
        "/home/alice/work/projects/local llm": "home-alice-work-projects-local\\x20llm",
        "/home/alice/work/projects/my-tool": "home-alice-work-projects-my\\x2dtool",
        "/home/alice/work/專案": "home-alice-work-\\xe5\\xb0\\x88\\xe6\\xa1\\x88",
        "/home/alice/.hidden": "home-alice-.hidden",
        "/.dot": "\\x2edot",
        "/a/b/": "a-b",
        "//a//b": "a-b",
        "/": "-",
        "/a\\b": "a\\x5cb",
        "/x:y_z.w": "x:y_z.w",
    }

    def test_known(self):
        for path, want in self.CASES.items():
            with self.subTest(path=path):
                self.assertEqual(systemd_escape_path(path), want)

    @unittest.skipUnless(shutil.which("systemd-escape"), "沒有 systemd-escape")
    def test_matches_real_systemd_escape(self):
        paths = list(self.CASES) + ["/home/alice/work/projects/local llm/中文 資料夾", "/home/alice/A B/c-d/e.f"]
        for p in paths:
            with self.subTest(path=p):
                real = subprocess.run(["systemd-escape", "--path", p], capture_output=True, text=True).stdout.strip()
                self.assertEqual(systemd_escape_path(p), real)

    def test_roundtrip(self):
        for p in ("/home/alice/work/projects/local llm", "/home/alice/work/專案/a-b", "/a/.b"):
            self.assertEqual(systemd_unescape_path(systemd_escape_path(p)), p)

    def test_unit_name(self):
        inst = instance_for_dir("/home/alice/work/projects/ledger")
        self.assertEqual(unit_for_instance(inst), "cchub-rc@home-alice-work-projects-ledger.service")

    def test_too_long(self):
        with self.assertRaises(CchubError):
            instance_for_dir("/home/alice/" + "中" * 40)


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.h = helpers.TempHome()
        self.cfg = load_config(self.h.paths)
        for d in ("work/projects/foo", "work/projects/My Proj", "work/projects/dup", "work/dup",
                  "work/projects/foo/.claude/worktrees/w1"):
            self.h.mkdir(*d.split("/"))
        os.symlink("/etc", os.path.join(self.h.project, "link-etc"))

    def tearDown(self):
        self.h.cleanup()

    def r(self, t):
        return resolve_target(t, self.cfg, self.h.home)

    def test_name_lookup(self):
        t = self.r("foo")
        self.assertEqual(t.path, os.path.join(self.h.project, "foo"))
        self.assertFalse(t.is_entry)

    def test_space_and_caps(self):
        self.assertEqual(self.r("My Proj").path, os.path.join(self.h.project, "My Proj"))

    def test_ambiguous_lists_all(self):
        with self.assertRaises(CchubError) as cm:
            self.r("dup")
        msg = str(cm.exception)
        self.assertIn(os.path.join(self.h.project, "dup"), msg)
        self.assertIn(os.path.join(self.h.home, "work", "dup"), msg)

    def test_absolute(self):
        self.assertEqual(self.r(os.path.join(self.h.project, "foo")).path, os.path.join(self.h.project, "foo"))

    def test_entry_forms(self):
        for t in ("entry", "hub", "projects", self.h.project, self.h.project + "/"):
            with self.subTest(t=t):
                self.assertTrue(self.r(t).is_entry)

    def test_control_chars_and_edge_whitespace_rejected(self):
        for t in ("foo\n", "foo\t", "foo\r", "\u3000foo", " foo", "foo ", "fo\u200bo", "foo\x00",
                  os.path.join(self.h.project, "foo") + "\n"):
            with self.subTest(t=repr(t)), self.assertRaises(CchubError):
                self.r(t)

    def test_config_paths_reject_control_chars(self):
        from cchub.paths import load_config
        self.h.update_config(projects_root=self.h.project + "\n")
        with self.assertRaises(CchubError) as cm:
            load_config(self.h.paths)
        self.assertIn("路徑無效", str(cm.exception))

    def test_rejections(self):
        wt = os.path.join(self.h.project, "foo", ".claude", "worktrees", "w1")
        for t in ("/etc", "~", self.h.home, "../x", "foo/bar", "..", ".", "nonexistent", wt,
                  "link-etc", os.path.join(self.h.home, "work"), "/"):
            with self.subTest(t=t), self.assertRaises(CchubError):
                self.r(t)


if __name__ == "__main__":
    unittest.main()
