import os, sys, tempfile, time, unittest, subprocess, threading
os.environ["ROUNDTABLE_LOG_DIR"] = tempfile.mkdtemp(prefix="rt_sq_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import room as R, agents, common, web, workspace as ws
ROOM = None   # 这个测试文件当前在用的会议


def new_room(title, ann="", wd=None):
    global ROOM
    ROOM = R.rooms.create(title, ann, wd)
    return ROOM.id

calls = []


def fake(ai, prompt, sid, imgs, mode=None, room=None):
    calls.append((ai, prompt))
    return True, "@用户 好了", sid or ("sid-" + ai), []


R.ai_caller = fake


def wait_idle():
    for _ in range(200):
        if not ROOM.busy_for():
            return
        time.sleep(0.05)


class T(unittest.TestCase):
    def setUp(self):
        calls.clear()
        R.ai_caller = fake
        new_room("sq%d" % time.time_ns())

    def test_stop_one_keeps_other(self):
        procs, started = {}, {a: threading.Event() for a in common.AIS}

        def slow(ai, prompt, sid, imgs, mode=None, room=None):
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
            ROOM.procs[ai] = p
            procs[ai] = p
            started[ai].set()
            p.wait()
            if p.returncode != 0:
                return False, "%s 出错了" % ai, sid, []
            return True, "@用户 %s 跑完了" % ai, sid or ("sid-" + ai), []

        R.ai_caller = slow
        ROOM.handle_message("你", "两个都干长活")
        self.assertTrue(started["claude"].wait(10) and started["codex"].wait(10))
        msg = ROOM.stop_ais(["claude"])
        self.assertIn("claude", msg)
        self.assertNotIn("codex", msg)
        wait_idle()
        self.assertNotEqual(procs["claude"].returncode, 0)
        self.assertEqual(procs["codex"].returncode, 0, "codex 不该被停")
        texts = [m["text"] for m in ROOM.messages]
        self.assertTrue(any("claude 被你打断了" in t for t in texts), texts)
        self.assertTrue(any("codex 跑完了" in t for t in texts), texts)

    def test_stop_one_drops_only_its_queue(self):
        hold = threading.Lock(); hold.acquire()
        real = dict(ROOM.ai_locks)
        ROOM.ai_locks["claude"] = hold
        try:
            ROOM.enqueue("claude", user=False)
            ROOM.enqueue("codex", user=False)
            ROOM.stop_ais(["codex"])
            self.assertIn("claude", ROOM.pending)
            self.assertNotIn("codex", ROOM.pending)
        finally:
            hold.release()
            ROOM.ai_locks.update(real)
            wait_idle()

    def test_quote_routes_to_quoted_ai(self):
        ROOM.handle_message("你", "@claude @codex 你们好")
        wait_idle()
        cm = [m for m in ROOM.messages if m["speaker"] == "claude"][-1]
        calls.clear()
        ROOM.handle_message("你", "这句什么意思", reply_to=ROOM.make_reply(cm["id"]))
        wait_idle()
        self.assertEqual([c[0] for c in calls], ["claude"])
        self.assertIn("（回复 [claude] 的“@用户 好了”）这句什么意思", calls[0][1])
        self.assertEqual(ROOM.messages[-2]["reply_to"]["id"], cm["id"]) if ROOM.messages[-2]["speaker"] == "你" else None

    def test_quote_with_mention_follows_mention(self):
        ROOM.handle_message("你", "@claude 在吗")
        wait_idle()
        cm = [m for m in ROOM.messages if m["speaker"] == "claude"][-1]
        calls.clear()
        ROOM.handle_message("你", "@codex 你看他这句", reply_to=ROOM.make_reply(cm["id"]))
        wait_idle()
        self.assertEqual([c[0] for c in calls], ["codex"])

    def test_make_reply_rules(self):
        ROOM.add_message("系统", "系统消息")
        self.assertIsNone(ROOM.make_reply(len(ROOM.messages) - 1))
        self.assertIsNone(ROOM.make_reply(9999))
        self.assertIsNone(ROOM.make_reply("abc"))
        ROOM.add_message("你", "x" * 500)
        r = ROOM.make_reply(len(ROOM.messages) - 1)
        self.assertEqual(len(r["text"]), R.QUOTE_LEN + 1)


if __name__ == "__main__":
    unittest.main()
