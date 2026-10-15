import os, sys, tempfile, threading, time, unittest, json
os.environ["ROUNDTABLE_LOG_DIR"] = tempfile.mkdtemp(prefix="rt_lg_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import roundtable as rt

threading.Thread(target=rt.worker, daemon=True).start()


def wait_idle():
    for _ in range(400):
        if not rt.busy_for():
            return
        time.sleep(0.05)


class Timeouts(unittest.TestCase):
    def test_idle_kills_silent_process(self):
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        t0 = time.time()
        rc, so, se, to = rt.run_stream(cmd, "", timeout=(2, 60))
        self.assertEqual(to, "idle")
        self.assertLess(time.time() - t0, 10)

    def test_output_keeps_it_alive(self):
        # 每秒说一句，总共 4 秒；没动静上限 2 秒也不该被停
        cmd = [sys.executable, "-u", "-c", "import time\nfor i in range(4):\n    print(i, flush=True); time.sleep(1)"]
        rc, so, se, to = rt.run_stream(cmd, "", timeout=(2, 60))
        self.assertIsNone(to)
        self.assertEqual(rc, 0)
        self.assertEqual(so.split(), ["0", "1", "2", "3"])

    def test_total_cap(self):
        cmd = [sys.executable, "-u", "-c", "import time\nwhile True:\n    print('.', flush=True); time.sleep(0.5)"]
        rc, so, se, to = rt.run_stream(cmd, "", timeout=(5, 3))
        self.assertEqual(to, "total")

    def test_texts(self):
        self.assertIn("20 分钟没有任何动静", rt.timeout_text("claude", "idle", "work"))
        self.assertIn("超过 120 分钟", rt.timeout_text("codex", "total", "work"))
        self.assertEqual(rt.timeout_for("discuss"), (600, 1800))


class Live(unittest.TestCase):
    def setUp(self):
        rt.new_meeting("lg%d" % time.time_ns())

    def test_claude_text_block_goes_live(self):
        res = {}
        line = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "我在本机开始生图了"}], "usage": {}}})
        rt.parse_claude_line(line, [], res)
        self.assertEqual(res.get("live"), "我在本机开始生图了")
        sub = json.dumps({"type": "assistant", "parent_tool_use_id": "x", "message": {"content": [{"type": "text", "text": "子助手的话"}]}})
        res2 = {}
        rt.parse_claude_line(sub, [], res2)
        self.assertNotIn("live", res2, "子助手的话不算")

    def test_live_shown_while_running_then_cleared(self):
        gate, inside = threading.Event(), threading.Event()

        def slow(ai, prompt, sid, imgs, mode=None):
            res = {"live": "正在跑第一步"}
            rt.push_live(ai, res)
            inside.set()
            gate.wait(10)
            return True, "@用户 好了", sid or "sid-" + ai, []

        rt.ai_caller = slow
        rt.handle_message("你", "@claude 干活")
        self.assertTrue(inside.wait(5))
        st = rt.api_state()
        self.assertEqual(st["live"]["claude"]["text"], "正在跑第一步")
        self.assertIn("secs", st["live"]["claude"])
        gate.set()
        wait_idle()
        self.assertNotIn("claude", rt.api_state()["live"])
        self.assertFalse(any("正在跑第一步" in m["text"] for m in rt.messages), "中途的话不进会议记录")


class CodexImages(unittest.TestCase):
    def setUp(self):
        rt.new_meeting("ci%d" % time.time_ns())
        self.img = os.path.join(tempfile.mkdtemp(), "ig_abc.png")
        with open(self.img, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")

    def test_generated_image_attached_once(self):
        orig = rt.codex_server.run_turn

        def fake_turn(sid, prompt, images, mode, model, effort, edits, res):
            res["gen_images"] = [self.img]
            return True, "@用户 画好了\n【发送】%s" % self.img, sid or "sid-codex"   # 它自己也写了同一张

        rt.codex_server.run_turn = fake_turn
        try:
            ok, out, sid = rt.call_codex_appserver("p", None, (), "discuss", "", "", [], {})
        finally:
            rt.codex_server.run_turn = orig
        self.assertTrue(ok)
        rt.handle_message("codex", out, origin="room")
        m = [x for x in rt.messages if x["speaker"] == "codex"][-1]
        self.assertEqual(m["text"], "@用户 画好了")
        self.assertEqual(len(m["attachments"]), 1, "同一张只发一次")

    def test_generation_failure_reported(self):
        orig = rt.codex_server.run_turn

        def fake_turn(sid, prompt, images, mode, model, effort, edits, res):
            res["gen_fail"] = ["生图额度用完了"]
            return True, "@用户 没画出来", sid or "sid-codex"

        rt.codex_server.run_turn = fake_turn
        try:
            ok, out, sid = rt.call_codex_appserver("p", None, (), "discuss", "", "", [], {})
        finally:
            rt.codex_server.run_turn = orig
        self.assertIn("（生图额度用完了）", out)

    def test_image_generation_not_disabled(self):
        self.assertNotIn("image_generation", rt.CODEX_DISCUSS_OFF)

    def test_rules(self):
        pre = rt.preamble("work")
        for k in ("codex 自带生图", "同一时间只能一个人用", "20 分钟", "别只描述"):
            self.assertIn(k, pre)


if __name__ == "__main__":
    unittest.main()
