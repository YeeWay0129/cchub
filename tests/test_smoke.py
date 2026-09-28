"""N7：正式進入點 bin/cchub 的 smoke test。

在 `env -i PATH=/usr/bin:/bin` 下執行 `--help`、`doctor`、`ls`、`install --dry-run`，確認結束碼 0 而且不寫任何檔。
這些都是唯讀指令，對象是真實的家目錄（Paths.default()，D9 之後不再能用環境變數換掉）：
- 有 strace 時，整個程序樹在 strace 下執行，斷言沒有任何「成功的寫入類系統呼叫」（-z 只列成功的呼叫）；
- 另外比對 cchub 相關目錄與本 repo 的快照（repo 裡的 __pycache__ 由測試程式自己產生，不算）。
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

import helpers

BIN = os.path.join(helpers.REPO, "bin", "cchub")
COMMANDS = (["--help"], ["doctor"], ["ls"], ["install", "--dry-run"])
TRACE = ("openat,open,creat,rename,renameat,renameat2,mkdir,mkdirat,unlink,unlinkat,rmdir,symlink,symlinkat,"
         "link,linkat,chmod,fchmodat,truncate,utimensat")
WRITE_RE = re.compile(r"O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|^\d+\s+(?:rename|mkdir|unlink|rmdir|symlink|link|chmod|"
                      r"fchmodat|truncate|utimensat)")


def watched_paths() -> list[str]:
    home = os.path.expanduser("~")
    return [helpers.REPO] + [os.path.join(home, p) for p in (
        ".config/cchub", ".local/state/cchub", ".local/share/cchub", ".config/systemd/user",
        ".claude/skills", ".local/bin", ".claude/settings.json")]


def snapshot_all() -> dict:
    snap = {}
    for p in watched_paths():
        if not os.path.lexists(p):
            snap[p] = None
        elif os.path.isdir(p) and not os.path.islink(p):
            snap.update({k: v for k, v in helpers.snapshot(p).items() if "__pycache__" not in k})
        else:
            st = os.lstat(p)
            snap[p] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return snap


def successful_writes(trace_file: str) -> list[str]:
    with open(trace_file, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    out = []
    for ln in lines:
        if not WRITE_RE.search(ln):
            continue
        m = re.search(r'"([^"]*)"', ln)
        path = m.group(1) if m else ""
        if path.startswith(("/dev/", "/proc/")):
            continue
        out.append(ln)
    return out


class EntryPointSmokeTest(unittest.TestCase):
    def test_readonly_commands_exit_0_and_write_nothing(self):
        strace = shutil.which("strace")
        tmp = tempfile.mkdtemp(prefix="cchub-smoke-")
        self.addCleanup(shutil.rmtree, tmp, True)
        before = snapshot_all()
        for args in COMMANDS:
            with self.subTest(args=args):
                base = ["env", "-i", "PATH=/usr/bin:/bin", BIN, *args]
                trace = os.path.join(tmp, "trace-" + "-".join(a.strip("-") for a in args) + ".txt")
                cmd = [strace, "-f", "-qq", "-z", "-e", f"trace={TRACE}", "-o", trace, *base] if strace else base
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
                self.assertEqual(r.returncode, 0, (r.stdout[-1500:] + "\n" + r.stderr[-1500:]))
                self.assertNotIn("Traceback", r.stderr)
                self.assertTrue(r.stdout.strip(), "沒有輸出")
                if strace:
                    with open(trace, encoding="utf-8", errors="replace") as f:
                        opens = sum(1 for ln in f if "openat(" in ln)
                    self.assertGreater(opens, 10, "strace 沒有記到東西（檢查會變成空轉）")
                    self.assertEqual(successful_writes(trace), [], f"{args} 有寫入")
        self.assertEqual(snapshot_all(), before)

    def test_entry_point_never_writes_bytecode(self):
        """不受快取狀態影響：把 bytecode 快取導到一個空資料夾，執行後它必須還是空的。"""
        prefix = tempfile.mkdtemp(prefix="cchub-pycache-")
        self.addCleanup(shutil.rmtree, prefix, True)
        for args in (["--help"], ["ls"]):
            r = subprocess.run(["env", "-i", "PATH=/usr/bin:/bin", f"PYTHONPYCACHEPREFIX={prefix}", BIN, *args],
                               capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        # 空的快取前綴會讓 Python 啟動時重寫標準函式庫的 .pyc（那發生在 bin/cchub 執行之前，與 cchub 無關），
        # 所以只看本 repo 對應的子目錄
        mine = os.path.join(prefix, helpers.REPO.lstrip("/"))
        written = [os.path.join(dp, f) for dp, _, fs in os.walk(mine) for f in fs]
        self.assertEqual(written, [], "bin/cchub 寫了 cchub 的 bytecode 快取")

    def test_help_mentions_brief_stdin_only(self):
        r = subprocess.run(["env", "-i", "PATH=/usr/bin:/bin", BIN, "new", "--help"], capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--brief-stdin", r.stdout)
        self.assertNotRegex(r.stdout, r"--brief(?!-stdin)")


if __name__ == "__main__":
    unittest.main()
