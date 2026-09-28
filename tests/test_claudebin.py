"""選 claude 執行檔（版本最高者）、清掉會影響 CLI 的環境變數。"""

import os
import unittest

import helpers
from cchub.claudebin import clean_env, find_candidates, parse_version, select_claude


def make_exec(path, mode=0o755):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(path, mode)


class SelectTest(unittest.TestCase):
    def setUp(self):
        self.h = helpers.TempHome()
        self.native = self.h.paths.native_cli_root
        self.desktop = self.h.paths.desktop_cli_root

    def tearDown(self):
        self.h.cleanup()

    def sel(self):
        return select_claude(self.native, self.desktop)

    def test_highest_across_roots(self):
        make_exec(os.path.join(self.desktop, "2.1.247", "claude"))
        make_exec(os.path.join(self.desktop, "2.1.280", "claude"))
        make_exec(os.path.join(self.native, "2.1.282"))
        make_exec(os.path.join(self.native, "2.1.283"))
        best, warns = self.sel()
        self.assertEqual(best.path, os.path.join(self.native, "2.1.283"))
        self.assertEqual(best.version_str, "2.1.283")
        self.assertEqual(warns, [])

    def test_numeric_compare(self):
        make_exec(os.path.join(self.native, "2.1.99"))
        make_exec(os.path.join(self.desktop, "2.1.100", "claude"))
        best, _ = self.sel()
        self.assertEqual(best.version, (2, 1, 100))
        self.assertTrue(os.path.isabs(best.path))
        make_exec(os.path.join(self.native, "2.2.0"))
        self.assertEqual(self.sel()[0].version, (2, 2, 0))

    def test_skips_invalid(self):
        make_exec(os.path.join(self.native, "9.9.9"), mode=0o644)      # 不能執行
        make_exec(os.path.join(self.native, "latest"))
        make_exec(os.path.join(self.native, "3.0.0.tmp"))
        os.makedirs(os.path.join(self.desktop, "8.0.0"))                # 沒有 claude
        make_exec(os.path.join(self.native, "2.1.290"))
        cands = find_candidates(self.native, self.desktop)
        self.assertEqual([c.version for c in cands], [(2, 1, 290)])

    def test_tie_prefers_native(self):
        make_exec(os.path.join(self.desktop, "2.1.290", "claude"))
        make_exec(os.path.join(self.native, "2.1.290"))
        self.assertEqual(self.sel()[0].source, "native")

    def test_old_version_warns(self):
        make_exec(os.path.join(self.desktop, "2.1.280", "claude"))
        best, warns = self.sel()
        self.assertEqual(best.version_str, "2.1.280")
        self.assertEqual(len(warns), 1)
        self.assertIn("2.1.281", warns[0])
        self.assertIn("Artifact", warns[0])

    def test_none(self):
        best, warns = self.sel()
        self.assertIsNone(best)
        self.assertIn("找不到", warns[0])

    def test_parse_version(self):
        self.assertEqual(parse_version("2.1.283"), (2, 1, 283))
        for bad in ("2.1", "v2.1.3", "2.1.3-beta", "", "a.b.c", "2.1.283\n", " 2.1.283", "2.1.283 "):
            self.assertIsNone(parse_version(bad))           # 一律 fullmatch：結尾的 \n 也不能過

    def test_version_dir_with_newline_skipped(self):
        make_exec(os.path.join(self.native, "9.9.9\n"))
        make_exec(os.path.join(self.native, "2.1.290"))
        self.assertEqual(self.sel()[0].version, (2, 1, 290))


class EnvTest(unittest.TestCase):
    def test_clean_env(self):
        env = {
            "HOME": "/h", "PATH": "/usr/bin", "LANG": "C.UTF-8", "DISABLE_AUTOUPDATER": "1",
            "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CONFIG_DIR": "/x",
            "ANTHROPIC_API_KEY": "fake", "ANTHROPIC_BASE_URL": "http://x", "anthropic_model": "m",
            "USE_LOCAL_OAUTH": "1", "USE_STAGING_OAUTH": "1", "MY_CLAUDE": "keep", "XDG_RUNTIME_DIR": "/run/user/1000",
        }
        out = clean_env(env)
        self.assertEqual(sorted(out), ["DISABLE_AUTOUPDATER", "HOME", "LANG", "MY_CLAUDE", "PATH", "XDG_RUNTIME_DIR"])
        self.assertEqual(out["HOME"], "/h")
        self.assertIn("CLAUDECODE", env)        # 不改原本的 dict


if __name__ == "__main__":
    unittest.main()
