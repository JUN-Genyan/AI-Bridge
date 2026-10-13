import os, sys, tempfile, time, unittest
os.environ["ROUNDTABLE_LOG_DIR"] = tempfile.mkdtemp(prefix="rt_board_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import roundtable as rt

calls = []
reply = {"text": "@用户 好了"}


def fake(ai, prompt, sid, imgs, mode=None):
    calls.append((ai, mode, prompt))
    rt.note_ctx(ai, {"ctx_used": 50000, "ctx_win": 200000})
    return True, reply["text"], sid or ("sid-" + ai), []


rt.ai_caller = fake
rt.snapshot_tree = lambda: "tree"
rt.record_step = lambda *a, **k: None


def wait_idle():
    for _ in range(100):
        if not rt.busy_for():
            return
        time.sleep(0.05)


class T(unittest.TestCase):
    def setUp(self):
        calls.clear()
        reply["text"] = "@用户 好了"
        rt.new_meeting("board%d" % time.time_ns())

    def test_ai_writes_board(self):
        reply["text"] = "@用户 我记一下\n【待办】改存档格式\n【公告】周五前别动 core\n```\n【待办】代码块里的不算\n```"
        rt.handle_message("你", "@claude 记下来")
        wait_idle()
        b = rt.meta["board"]
        self.assertEqual([(i["kind"], i["text"], i["by"]) for i in b],
                         [("todo", "改存档格式", "claude"), ("note", "周五前别动 core", "claude")])
        shown = [m for m in rt.messages if m["speaker"] == "claude"][-1]["text"]
        self.assertNotIn("【公告】", shown.split("```")[0])
        self.assertIn("代码块里的不算", shown)
        note = [m for m in rt.messages if m["speaker"] == "系统" and "看板" in m["text"]][-1]
        self.assertEqual(note.get("not_for"), "claude")
        # codex 下一轮能看到系统通知
        rt.handle_message("你", "@codex 看看")
        wait_idle()
        self.assertIn("加了待办 #1", calls[-1][2])
        self.assertIn("看板：\n#1 [待办] 改存档格式", calls[-1][2])   # 第一次叫 codex 带全量看板

    def test_done_and_delete(self):
        rt.board_op("你", "add", "todo", "a")
        rt.board_op("你", "add", "todo", "b")
        reply["text"] = "【完成】1\n【删】2"
        rt.handle_message("你", "@claude 收一下")
        wait_idle()
        self.assertEqual([(i["id"], i["done"]) for i in rt.meta["board"]], [(1, True)])
        # 只有看板命令时不留空消息
        self.assertFalse([m for m in rt.messages if m["speaker"] == "claude" and not m["text"]])

    def test_bad_id(self):
        reply["text"] = "【完成】99"
        rt.handle_message("你", "@claude x")
        wait_idle()
        self.assertTrue(any("没改成" in m["text"] for m in rt.messages if m["speaker"] == "系统"))

    def test_ctx_saved(self):
        rt.handle_message("你", "@claude 你好")
        wait_idle()
        self.assertEqual(rt.meta["ctx"]["claude"]["used"], 50000)
        self.assertEqual(rt.api_state()["ais"]["claude"]["ctx"]["window"], 200000)
        self.assertIsNone(rt.api_state()["ais"]["codex"]["ctx"])

    def test_parse_claude_usage(self):
        res = {}
        rt.parse_claude_line('{"type":"assistant","message":{"usage":{"input_tokens":2,"cache_read_input_tokens":1000,"cache_creation_input_tokens":10,"output_tokens":5},"content":[]}}', [], res)
        rt.parse_claude_line('{"type":"assistant","parent_tool_use_id":"x","message":{"usage":{"input_tokens":999999},"content":[]}}', [], res)
        rt.parse_claude_line('{"type":"result","result":"ok","modelUsage":{"a":{"inputTokens":1,"cacheReadInputTokens":1,"contextWindow":200000},"b":{"inputTokens":5,"cacheReadInputTokens":9000,"contextWindow":1000000}}}', [], res)
        self.assertEqual(res["ctx_used"], 1017)
        self.assertEqual(res["ctx_win"], 1000000)

    def test_rules_include_board(self):
        self.assertIn("【待办】", rt.mode_text("discuss"))
        self.assertIn("【待办】", rt.preamble("work"))


if __name__ == "__main__":
    unittest.main()
