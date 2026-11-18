import copy
import unittest

from mini_agent.engine import Engine, INTERRUPTED, SYSTEM, _cost, _summary


def call(i, name="read_file", args='{"path":"a"}'):
    return {"id": str(i), "name": name, "arguments": args}


def reply(*calls, text="", finish="stop"):
    return {"role": "assistant", "content": text, "calls": list(calls),
            "finish": finish, "input_tokens": 10, "output_tokens": 4}


class Provider:
    def __init__(self, script):
        self.script = iter(script)
        self.requests = []

    def complete(self, messages, schemas, emit, log):
        self.requests.append(copy.deepcopy(messages))
        response = next(self.script)
        if isinstance(response, BaseException):
            emit("已接收的部分回复")
            raise response
        emit(response["content"])
        return response


class Tools:
    schemas = []

    def __init__(self, interrupt_at=0):
        self.attempts = []
        self.interrupt_at = interrupt_at

    def execute(self, name, arguments):
        self.attempts.append((name, arguments))
        if len(self.attempts) == self.interrupt_at:
            raise KeyboardInterrupt
        return "工具返回的数据", True


def assert_legal(test, messages):
    test.assertEqual(messages[0]["role"], "system")
    next_speaker = "user"
    pending = []
    for message in messages[1:]:
        if pending:
            test.assertEqual(message["role"], "tool")
            test.assertEqual(message["id"], pending.pop(0))
            continue
        test.assertEqual(message["role"], next_speaker)
        if next_speaker == "user":
            next_speaker = "assistant"
        else:
            pending = [c["id"] for c in message.get("calls", [])]
            next_speaker = "assistant" if pending else "user"
    test.assertFalse(pending)


class EngineTests(unittest.TestCase):
    def make(self, script=(), history=None, tools=None, **kwargs):
        self.saved, self.events, self.chunks, self.status = [], [], [], []
        return Engine(
            Provider(script), tools or Tools(), history if history is not None else [],
            lambda history: self.saved.append(copy.deepcopy(history)),
            lambda *event: self.events.append(event), self.chunks.append,
            status=self.status.append, **kwargs,
        )

    def test_multicall_context_and_usage(self):
        engine = self.make([reply(call(1), call(2)), reply(text="完成"), reply(text="再来一次")])
        self.assertEqual(engine.turn("第一次"), "完成")
        self.assertEqual(self.status[-1], "[用量] 输入=20 输出=8")
        engine.turn("第二次")
        for request in engine.provider.requests:
            assert_legal(self, request)
        self.assertEqual([m["id"] for m in engine.history if m["role"] == "tool"], ["1", "2"])
        self.assertEqual(self.chunks, ["", "完成", "再来一次"])

    def test_cancel_three_calls_and_resume_from_crash_checkpoint(self):
        engine = self.make([reply(call(1), call(2), call(3))], tools=Tools(2))
        with self.assertRaises(KeyboardInterrupt):
            engine.turn("执行")
        assert_legal(self, engine.history)
        crash = next(s for s in self.saved if s[-1]["role"] == "tool")
        crash[0]["content"] = "Legacy system prompt"
        recovered = self.make([reply(text="已继续")], history=crash)
        self.assertEqual([m["content"] for m in recovered.history if m["role"] == "tool"],
                         ["工具返回的数据", INTERRUPTED, INTERRUPTED])
        self.assertEqual(recovered.turn("检查状态"), "已继续")
        assert_legal(self, recovered.provider.requests[0])
        self.assertFalse(recovered.tools.attempts)
        self.assertEqual(recovered.provider.requests[0][0]["content"], SYSTEM)

    def test_partial_stream_discarded_on_interrupt(self):
        engine = self.make([KeyboardInterrupt(), reply(text="就绪")])
        with self.assertRaises(KeyboardInterrupt):
            engine.turn("开始")
        self.assertIn("已接收的部分回复", self.chunks)
        self.assertNotIn("已接收的部分回复", str(engine.history))
        engine.turn("再来一次")
        assert_legal(self, engine.history)

    def test_limit_and_textual_and_truncated_tool_call(self):
        engine = self.make([
            reply(text='{"name":"run_shell","arguments":{"command":"example"}}'),
            reply(call("x", args='{"path":'), finish="length"),
        ], max_steps=2)
        self.assertIn("2 次模型调用", engine.turn("执行"))
        self.assertFalse(engine.tools.attempts)
        self.assertIn("截断", engine.history[-2]["content"])
        assert_legal(self, engine.history)

    def test_empty_final_reply_stays_valid_on_next_turn(self):
        from mini_agent.providers import anthropic_request

        engine = self.make([reply(text=""), reply(text="好的")])
        self.assertIn("未返回文本", engine.turn("开始"))
        engine.turn("继续")
        request = anthropic_request(engine.provider.requests[-1], [])
        self.assertTrue(all(m["content"] for m in request["messages"]))

    def test_compaction_after_textual_tool_correction_preserves_earlier_plan(self):
        engine = self.make(context_window=2400)
        current = {"role": "user", "content": "修复应用"}
        engine.history.extend([
            current, reply(call(1), text="TODO:\n- 运行冒烟测试"),
            {"role": "tool", "id": "1", "content": "x" * 20000, "ok": True},
            reply(text='{"name":"read_file","arguments":{}}'),
            {"role": "user", "content": "请使用原生工具调用执行刚才提出的操作。"}
        ])
        compact = engine._request(current)
        assert_legal(self, compact)
        self.assertIn("运行冒烟测试", compact[1]["content"])
        self.assertEqual(compact[-1]["content"], "请使用原生工具调用执行刚才提出的操作。")
        self.assertIn('"name":"read_file"', compact[-2]["content"])

    def test_repeated_cycles_length_one_two_three(self):
        for cycle in (1, 2, 3):
            with self.subTest(size=cycle):
                engine = self.make([reply(call(i, args='{"path":"%d"}' % (i % cycle)))
                                    for i in range(3 * cycle)])
                self.assertIn("循环重复", engine.turn("读取"))
                self.assertEqual(len(engine.tools.attempts), cycle * 3 - 1)
                assert_legal(self, engine.history)

    def test_compaction_cut_inside_parallel_results(self):
        current = {"role": "user", "content": "修复应用并验证测试结果。"}
        history = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "之前的任务"}, reply(text="z" * 5000), current,
            reply(call("first", "write_file"), call("second"), text="待办：运行测试。"),
            {"role": "tool", "id": "first", "content": "已修改", "ok": True},
            {"role": "tool", "id": "second", "content": "大段内容" * 3000, "ok": True},
        ]
        engine = self.make()
        engine.history[:] = history
        # 将预算设在仅够保留第二个结果的位置，验证压缩时整批调用一起取舍。
        note = _summary(history[1:], current) + "\n\n当前任务：\n" + current["content"]
        budget = sum(map(_cost, [history[0], {"role": "user", "content": note},
                                   history[-1]])) + _cost(engine.tools.schemas)
        engine.context_window = (5 * budget + 3) // 4
        compact = engine._request(current)
        assert_legal(self, compact)
        self.assertEqual(len(compact), 2)
        for fact in ("修复应用", '"a": "已确认"', "待办：运行测试。"):
            self.assertIn(fact, compact[1]["content"])


if __name__ == "__main__":
    unittest.main()
