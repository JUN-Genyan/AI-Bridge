import os, sys, tempfile, time, unittest, threading
os.environ["ROUNDTABLE_LOG_DIR"] = tempfile.mkdtemp(prefix="rt_cp_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import room as R, agents, common, web, workspace as ws
ROOM = None   # 这个测试文件当前在用的会议


def new_room(title, ann="", wd=None):
    global ROOM
    ROOM = R.rooms.create(title, ann, wd)
    return ROOM.id

calls = []
compacted = []


def fake(ai, prompt, sid, imgs, mode=None, room=None):
    calls.append(ai)
    return True, "@用户 好了", sid or ("sid-" + ai), []


def fake_compact(ai, sid, room=None):
    compacted.append(ai)
    return True


R.ai_caller = fake
R.ai_native_compact = fake_compact


def wait_idle():
    for _ in range(200):
        if not ROOM.busy_for():
            return
        time.sleep(0.05)


class T(unittest.TestCase):
    def setUp(self):
        calls.clear()
        compacted.clear()
        R.ai_caller = fake
        R.ai_native_compact = fake_compact
        new_room("cp%d" % time.time_ns())
        ROOM.handle_message("你", "@claude @codex 先建会话")
        wait_idle()
        calls.clear()

    def notes(self):
        return [m["text"] for m in ROOM.messages if m["speaker"] == "系统"]

    def test_compact_only_named(self):
        self.assertEqual(ROOM.request_compact(["codex"]), ["codex"])
        wait_idle()
        self.assertEqual(compacted, ["codex"])
        self.assertTrue(any(t == "codex 压缩好了" for t in self.notes()), self.notes())

    def test_slash_compact_with_name(self):
        ROOM.handle_message("你", "/compact claude")
        wait_idle()
        self.assertEqual(compacted, ["claude"])
        self.assertEqual(calls, [], "/compact 不该叫 AI")

    def test_slash_compact_both_each_reports(self):
        ROOM.handle_message("你", "/compact")
        wait_idle()
        self.assertEqual(sorted(compacted), ["claude", "codex"])
        n = self.notes()
        self.assertTrue(any(t == "claude 压缩好了" for t in n), n)
        self.assertTrue(any(t == "codex 压缩好了" for t in n), n)

    def test_other_ai_free_while_compacting(self):
        gate, started = threading.Event(), threading.Event()

        def slow_compact(ai, sid, room=None):
            started.set()
            gate.wait(10)
            compacted.append(ai)
            return True

        R.ai_native_compact = slow_compact
        ROOM.request_compact(["claude"])
        self.assertTrue(started.wait(5))
        self.assertTrue(ROOM.busy_for("claude"))
        self.assertFalse(ROOM.busy_for("codex"))
        ROOM.handle_message("你", "@codex 你照常说")
        for _ in range(100):
            if "codex" in calls:
                break
            time.sleep(0.05)
        self.assertIn("codex", calls, "claude 在压缩时 codex 应该照常能叫")
        self.assertIn("compact:claude", ROOM.state["running"])
        self.assertEqual(ROOM.request_compact(["claude"]), [], "已经在压的不重复排")
        gate.set()
        wait_idle()
        self.assertEqual(compacted, ["claude"])

    def test_codex_failure_keeps_session(self):
        R.ai_native_compact = lambda ai, sid, room=None: False
        sid = ROOM.meta["codex_session"]
        ROOM.request_compact(["codex"])
        wait_idle()
        self.assertEqual(ROOM.meta["codex_session"], sid, "codex 压不成不该换会话")
        self.assertTrue(any("codex 没压成" in t for t in self.notes()), self.notes())


if __name__ == "__main__":
    unittest.main()
