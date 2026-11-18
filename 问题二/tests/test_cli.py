import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from mini_agent.storage import Session
from mini_agent.cli import interact, options


class TerminalTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / "sessions"
        self.workspace = Path(temp.name) / "project"
        self.workspace.mkdir()
        self.script = Path(temp.name) / "mock.json"
        self.env_file = Path(temp.name) / ".env"
        self.env_file.write_text("MINI_AGENT_PROVIDER=mock")
        self.env = dict(os.environ, MINI_AGENT_HOME=str(self.home), PYTHONUTF8="1",
                        MINI_AGENT_API_KEY="test-secret-for-redaction")

    def run_cli(self, script, *args, stdin=""):
        self.script.write_text(json.dumps(script), encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "mini_agent", "--workspace", str(self.workspace),
             "--env-file", str(self.env_file), "--provider", "mock", "--mock-script", str(self.script), *args],
            input=stdin, env=self.env, encoding="utf-8", capture_output=True, timeout=20)

    def test_approval_resume_and_log_redaction(self):
        script = [{"calls": [{"id": "a", "name": "write_file",
                              "arguments": {"path": "note.txt", "content": "你好"}}]},
                  {"content": "本轮结束"}]
        first = self.run_cli(script, "用 test-secret-for-redaction 验证脱敏", stdin="n\n")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertFalse((self.workspace / "note.txt").exists())
        session, = self.home.glob("*.json")
        trace = session.with_suffix(".jsonl").read_text(encoding="utf-8")
        self.assertTrue(trace.strip())
        self.assertNotIn("test-secret-for-redaction", trace)
        self.assertNotIn("test-secret-for-redaction", session.read_text())
        self.assertIn("本轮结束", first.stdout)
        second = self.run_cli(script, "--yes", "--resume", session.stem,
                              stdin="创建文件\n/exit\n")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((self.workspace / "note.txt").read_text(encoding="utf-8"), "你好")
        history = json.loads(session.read_text(encoding="utf-8"))["history"]
        self.assertEqual(sum(m["role"] == "user" for m in history), 2)

    def test_session_binding(self):
        first = self.run_cli([{"content": "已完成"}], "你好")
        self.assertEqual(first.returncode, 0, first.stderr)
        session, = self.home.glob("*.json")
        self.workspace = self.workspace.parent
        second = self.run_cli([], "--resume", session.stem, "继续")
        self.assertNotEqual(second.returncode, 0)

    def test_dotenv_workspace_override_and_resume(self):
        self.env_file.write_text("MINI_AGENT_PROVIDER=mock\nMINI_AGENT_WORKSPACE=new/project\n")
        configured = self.env_file.parent / "new" / "project"
        cmd = [sys.executable, "-m", "mini_agent", "--env-file", str(self.env_file)]
        def run(*args):
            result = subprocess.run([*cmd, *args, "列出文件"], env=self.env, encoding="utf-8",
                                    capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        self.assertFalse(configured.exists())
        self.assertIn(f"工作目录 {configured}\n", run())
        self.assertTrue(configured.is_dir())
        session, = self.home.glob("*.json")
        self.env_file.write_text("MINI_AGENT_PROVIDER=mock\nMINI_AGENT_WORKSPACE=已移动\n", encoding="utf-8")
        self.assertIn(f"工作目录 {configured}\n", run("--resume", session.stem))
        override = self.workspace / "sub" / "project"
        self.assertIn(f"工作目录 {override}\n", run("--workspace", str(override)))
        self.assertTrue(override.is_dir())

    def test_existing_workspace_file_is_preserved(self):
        target = self.workspace / "file.txt"
        target.write_text("保留", encoding="utf-8")
        result = self.run_cli([], "--workspace", str(target), "启动")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "保留")

    def test_default_config_and_redaction_of_all_configured_keys(self):
        self.env_file.write_text("MINI_AGENT_PROVIDER=mock\nANTHROPIC_API_KEY=unused-file-key\n")
        env = dict(self.env, MINI_AGENT_PROVIDER="anthropic", PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        result = subprocess.run(
            [sys.executable, "-m", "mini_agent", "--workspace", str(self.workspace),
             "列出文件 unused-file-key"], env=env, cwd=self.env_file.parent,
            encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("离线演示", result.stdout)
        trace, = self.home.glob("*.jsonl")
        for path in (trace, trace.with_suffix(".json")):
            self.assertNotIn("unused-file-key", path.read_text(encoding="utf-8"))

    def test_help_and_argument_errors_are_in_chinese(self):
        help_text = self.run_cli([], "--help")
        self.assertEqual(help_text.returncode, 0)
        for label in ("用法", "位置参数", "显示帮助并退出", "恢复指定会话"):
            self.assertIn(label, help_text.stdout)
        invalid = self.run_cli([], "--max-steps", "0")
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("--max-steps", invalid.stderr)
        self.assertIn("必须是正整数", invalid.stderr)

    def test_concurrent_resume_leaves_checkpoint_intact(self):
        first = self.run_cli([{"content": "已保存"}], "你好")
        self.assertEqual(first.returncode, 0, first.stderr)
        session, = self.home.glob("*.json")
        checkpoint = session.read_bytes()
        with Session(self.home, session.stem).lock():
            second = self.run_cli([], "--resume", session.stem, "从另一个进程写入")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("另一个进程", second.stderr)
        self.assertEqual(checkpoint, session.read_bytes())

    def test_key_fields_and_session_id(self):
        with self.assertRaises(ValueError):
            Session(self.home, "../elsewhere")
        store = Session(self.home, "a" * 32, ["private-value"])
        store.log("event", {"nested": [{"X-Api-Key": "other-value"}],
                            "content": "输入的密钥为 private-value"})
        trace = store.trace.read_text()
        self.assertNotIn("other-value", trace)
        self.assertNotIn("private-value", trace)

    def test_interrupted_round_returns_to_prompt_and_idle_interrupt_exits(self):
        engine = Mock()
        engine.turn.side_effect = [KeyboardInterrupt(), "已完成"]
        with patch("builtins.input", side_effect=["第一个任务", "第二个任务", KeyboardInterrupt()]):
            self.assertEqual(interact(options([]), engine, Mock()), 0)
        self.assertEqual([call.args for call in engine.turn.call_args_list], [("第一个任务",), ("第二个任务",)])
