import os, sys, tempfile, time, unittest, subprocess, threading
os.environ["ROUNDTABLE_LOG_DIR"] = tempfile.mkdtemp(prefix="rt_sq_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import roundtable as rt

calls = []


def fake(ai, prompt, sid, imgs, mode=None):
    calls.append((ai, prompt))
    return True, "@用户 好了", sid or ("sid-" + ai), []


rt.ai_caller = fake


def wait_idle():
    for _ in range(200):
        if not rt.busy_for():
            return
        time.sleep(0.05)


class T(unittest.TestCase):
    def setUp(self):
        calls.clear()
        rt.ai_caller = fake
        rt.new_meeting("sq%d" % time.time_ns())

    def test_stop_one_keeps_other(self):
        procs, started = {}, {a: threading.Event() for a in rt.AIS}

        def slow(ai, prompt, sid, imgs, mode=None):
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
            rt.live_procs[ai] = p
            procs[ai] = p
            started[ai].set()
            p.wait()
            if p.returncode != 0:
                return False, "%s 出错了" % ai, sid, []
            return True, "@用户 %s 跑完了" % ai, sid or ("sid-" + ai), []

        rt.ai_caller = slow
        rt.handle_message("你", "两个都干长活")
        self.assertTrue(started["claude"].wait(10) and started["codex"].wait(10))
        msg = rt.stop_ais(["claude"])
        self.assertIn("claude", msg)
        self.assertNotIn("codex", msg)
        wait_idle()
        self.assertNotEqual(procs["claude"].returncode, 0)
        self.assertEqual(procs["codex"].returncode, 0, "codex 不该被停")
        texts = [m["text"] for m in rt.messages]
        self.assertTrue(any("claude 被你打断了" in t for t in texts), texts)
        self.assertTrue(any("codex 跑完了" in t for t in texts), texts)

    def test_stop_one_drops_only_its_queue(self):
        hold = threading.Lock(); hold.acquire()
        real = dict(rt.ai_locks)
        rt.ai_locks["claude"] = hold
        try:
            rt.enqueue("claude", user=False)
            rt.enqueue("codex", user=False)
            rt.stop_ais(["codex"])
            self.assertIn("claude", rt.pending)
            self.assertNotIn("codex", rt.pending)
        finally:
            hold.release()
            rt.ai_locks.update(real)
            wait_idle()

    def test_quote_routes_to_quoted_ai(self):
        rt.handle_message("你", "@claude @codex 你们好")
        wait_idle()
        cm = [m for m in rt.messages if m["speaker"] == "claude"][-1]
        calls.clear()
        rt.handle_message("你", "这句什么意思", reply_to=rt.make_reply(cm["id"]))
        wait_idle()
        self.assertEqual([c[0] for c in calls], ["claude"])
        self.assertIn("（回复 [claude] 的“@用户 好了”）这句什么意思", calls[0][1])
        self.assertEqual(rt.messages[-2]["reply_to"]["id"], cm["id"]) if rt.messages[-2]["speaker"] == "你" else None

    def test_quote_with_mention_follows_mention(self):
        rt.handle_message("你", "@claude 在吗")
        wait_idle()
        cm = [m for m in rt.messages if m["speaker"] == "claude"][-1]
        calls.clear()
        rt.handle_message("你", "@codex 你看他这句", reply_to=rt.make_reply(cm["id"]))
        wait_idle()
        self.assertEqual([c[0] for c in calls], ["codex"])

    def test_make_reply_rules(self):
        rt.add_message("系统", "系统消息")
        self.assertIsNone(rt.make_reply(len(rt.messages) - 1))
        self.assertIsNone(rt.make_reply(9999))
        self.assertIsNone(rt.make_reply("abc"))
        rt.add_message("你", "x" * 500)
        r = rt.make_reply(len(rt.messages) - 1)
        self.assertEqual(len(r["text"]), rt.QUOTE_LEN + 1)


if __name__ == "__main__":
    unittest.main()
