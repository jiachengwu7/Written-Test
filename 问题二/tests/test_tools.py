import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_agent.tools import Tools


def python_command(*args):
    argv = [sys.executable, *args]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


class ToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.outer = Path(directory.name)
        self.root = self.outer / "work"
        self.root.mkdir()
        self.prompts = []
        self.tools = Tools(self.root, lambda text: self.prompts.append(text) is None)

    def call(self, name, **args):
        return self.tools.execute(name, json.dumps(args))

    def test_validation_rejects_invalid_json_missing_wrong_type_unknown_tool(self):
        for name, args, expected in [
            ("read_file", '{"path":', "JSON 格式无效"),
            ("missing", "{}", "未知工具：missing"),
            ("edit_file", "{}", "old_string：缺少必填字段"),
            ("read_file", '{"path":"a","start_line":"2"}', "start_line：必须是整数"),
            ("run_shell", '{"command":"x","timeout":3601}', "timeout：必须小于或等于 3600"),
            ("list_files", '{"extra":0}', "extra：不支持此字段"),
        ]:
            with self.subTest(name=name, args=args):
                message, success = self.tools.execute(name, args)
                self.assertFalse(success)
                self.assertIn(expected, message)
        self.assertEqual(6, len(self.tools.schemas))

    def test_create_numbered_read_and_edit(self):
        _, ok = self.call("write_file", path="sub/file", content="first\nsecond\n")
        self.assertTrue(ok)
        self.assertIn("+first", self.prompts[-1])
        self.assertEqual(("2: second", True), self.call("read_file", path="sub/file", start_line=2, end_line=2))
        self.assertTrue(self.call("edit_file", path="sub/file", old_string="second", new_string="third")[1])
        self.assertIn("-second", self.prompts[-1])
        self.assertEqual(b"first\nthird\n", (self.root / "sub/file").read_bytes())

    def test_denied_and_ambiguous_edits_do_not_modify(self):
        target = self.root / "file"
        target.write_bytes(b"aaa")
        for old, reason in [("aa", "多个位置"), ("unknown", "未找到")]:
            result, ok = self.call("edit_file", path="file", old_string=old, new_string="bad")
            self.assertFalse(ok)
            self.assertIn(reason, result)
        self.tools.confirm = lambda _: False
        for name, args in [
            ("write_file", dict(path="new/sub", content="x")),
            ("edit_file", dict(path="file", old_string="aaa", new_string="b")),
            ("run_shell", dict(command="echo no")),
        ]:
            self.assertIn("用户拒绝", self.call(name, **args)[0])
        self.assertFalse((self.root / "new").exists())
        self.assertEqual(b"aaa", target.read_bytes())

    def test_preserve_bom_and_mixed_line_endings(self):
        target = self.root / "file"
        original = b"\xef\xbb\xbfalpha\r\nbravo\ncharlie"
        target.write_bytes(original)
        self.assertTrue(self.call("edit_file", path="file", old_string="bravo", new_string="B")[1])
        self.assertEqual(original.replace(b"bravo", b"B"), target.read_bytes())

    def test_globs_grep_and_ignored_directories(self):
        for filename in ["a.py", "src/b.py", "src/c.txt", ".git/secrets.py", "node_modules/dep.py"]:
            path = self.root / filename
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"some\nmarker 234\n")
        result, ok = self.call("list_files", pattern="**/*.py")
        self.assertTrue(ok)
        self.assertEqual({"a.py", "src/b.py"}, set(result.splitlines()))
        self.assertEqual(("a.py", True), self.call("list_files", pattern="*.py"))
        result, ok = self.call("grep", pattern=r"marker \d{3}", glob="**/*.py")
        self.assertTrue(ok)
        self.assertEqual({"a.py:2: marker 234", "src/b.py:2: marker 234"}, set(result.splitlines()))

    def test_read_five_megabytes_and_continue(self):
        (self.root / "big").write_bytes((b"a" * 99 + b"\n") * 50000)
        result, ok = self.call("read_file", path="big")
        self.assertTrue(ok)
        self.assertLess(len(result), 12500)
        self.assertIn("50000 行", result)
        result, ok = self.call("read_file", path="big", start_line=50000)
        self.assertEqual((f"50000: {'a' * 99}", True), (result, ok))

    def test_workspace_paths(self):
        for path in ["../outside", str(self.outer / "outside"), r"C:\Windows\test", r"..\outside"]:
            self.assertFalse(self.call("write_file", path=path, content="no")[1], path)
        self.assertFalse((self.outer / "outside").exists())

    def test_directory_link_excluded_from_all_access(self):
        elsewhere = self.outer / "outside"
        elsewhere.mkdir()
        (elsewhere / "secret").write_text("marker")
        link = self.root / "link"
        try:
            link.symlink_to(elsewhere, target_is_directory=True)
        except OSError as exc:
            if os.name != "nt":
                raise
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(elsewhere)],
                                    capture_output=True)
            if result.returncode:
                self.skipTest(f"当前环境无法创建符号链接或目录联接（{exc}）")
        self.assertFalse(self.call("read_file", path="link/secret")[1])
        self.assertFalse(self.call("write_file", path="link/new", content="bad")[1])
        self.assertEqual(("", True), self.call("list_files", pattern="**/*"))
        self.assertEqual(("", True), self.call("grep", pattern="marker"))
        self.assertFalse((elsewhere / "new").exists())

    def test_shell_captures_both_streams_status_and_limits(self):
        code = ("import os,sys; print('x'*50000); "
                "print(os.environ.get('OPENAI_API_KEY','removed'),file=sys.stderr); sys.exit(7)")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "hidden-value"}):
            result, ok = self.call("run_shell", command=python_command("-c", code))
        self.assertFalse(ok)
        self.assertIn("退出码：7", result)
        self.assertIn("标准错误：\nremoved", result)
        self.assertIn("已截断，共 1 行", result)
        self.assertLess(len(result), 7000)

    def test_timeout_and_interrupt_terminate_grandchild(self):
        (self.root / "child.py").write_text(
            "from pathlib import Path\nimport time\nPath('ready').touch()\n"
            "time.sleep(2)\nPath('leaked').touch()\n"
        )
        (self.root / "parent.py").write_text(
            "import subprocess,sys,time\nsubprocess.Popen([sys.executable, 'child.py'])\ntime.sleep(10)\n"
        )
        command = python_command("parent.py")
        result, ok = self.call("run_shell", command=command, timeout=1)
        self.assertFalse(ok)
        self.assertIn("命令执行超时（1 秒）", result)
        self.assertTrue((self.root / "ready").exists())
        (self.root / "ready").unlink()
        original_wait = subprocess.Popen.wait

        def interrupt(proc, timeout=None):
            if timeout is None:
                return original_wait(proc)
            deadline = time.monotonic() + 5
            while not (self.root / "ready").exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.root / "ready").exists())
            raise KeyboardInterrupt

        with patch.object(subprocess.Popen, "wait", interrupt), self.assertRaises(KeyboardInterrupt):
            self.call("run_shell", command=command)
        time.sleep(2.1)
        self.assertFalse((self.root / "leaked").exists())
