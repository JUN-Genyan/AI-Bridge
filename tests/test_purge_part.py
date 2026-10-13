import os, sys, tempfile, time, unittest, json, threading
os.environ["ROUNDTABLE_LOG_DIR"] = tempfile.mkdtemp(prefix="rt_pp_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import roundtable as rt

FAKE_HOME = tempfile.mkdtemp(prefix="rt_home_")
rt.CLAUDE_HOME = FAKE_HOME          # 绝不碰真的 ~/.claude
codex_deleted = []
rt.delete_codex_thread = lambda sid: (codex_deleted.append(sid) or (True, ""))   # 不碰真的 codex
calls = []


def fake(ai, prompt, sid, imgs, mode=None):
    calls.append((ai, prompt))
    return True, "@用户 好了", sid or ("sid-%s-%d" % (ai, time.time_ns())), []


rt.ai_caller = fake


def wait_idle():
    for _ in range(200):
        if not rt.busy_for():
            return
        time.sleep(0.05)


def make_claude_files(sid):
    proj = os.path.join(FAKE_HOME, "projects", "D--Some-Proj")
    os.makedirs(os.path.join(proj, sid, "subagents"), exist_ok=True)
    open(os.path.join(proj, sid + ".jsonl"), "w").close()
    os.makedirs(os.path.join(FAKE_HOME, "file-history", sid), exist_ok=True)
    os.makedirs(os.path.join(FAKE_HOME, "todos"), exist_ok=True)
    open(os.path.join(FAKE_HOME, "todos", sid + "-agent-x.json"), "w").close()
    keep = os.path.join(proj, "other-session-1234.jsonl")
    open(keep, "w").close()
    return proj, keep


def wait_note(word):
    for _ in range(100):
        if any(word in m["text"] for m in rt.messages):
            return True
        time.sleep(0.05)
    return False


class T(unittest.TestCase):
    def test_purge_deletes_sessions(self):
        a = rt.new_meeting("要删的")
        rt.handle_message("你", "两个都说话")
        wait_idle()
        csid, xsid = rt.meta["claude_session"], rt.meta["codex_session"]
        rt.meta["old_sessions"]["claude"].append("old-claude-0001")
        rt.save_meta()
        proj, keep = make_claude_files(csid)
        make_claude_files("old-claude-0001")
        rt.new_meeting("留着的")
        ok, err = rt.trash_meeting(a)
        self.assertTrue(ok, err)
        self.assertTrue(os.path.exists(os.path.join(proj, csid + ".jsonl")), "进回收站时不删")
        ok, err = rt.purge_all()
        self.assertTrue(ok, err)
        self.assertTrue(wait_note("彻底删除"))
        self.assertFalse(os.path.exists(os.path.join(proj, csid + ".jsonl")))
        self.assertFalse(os.path.exists(os.path.join(proj, csid)))
        self.assertFalse(os.path.exists(os.path.join(FAKE_HOME, "file-history", csid)))
        self.assertFalse(os.path.exists(os.path.join(FAKE_HOME, "todos", csid + "-agent-x.json")))
        self.assertFalse(os.path.exists(os.path.join(proj, "old-claude-0001.jsonl")))
        self.assertTrue(os.path.exists(keep), "别的会话不能动")
        self.assertIn(xsid, codex_deleted)

    def test_shared_session_kept(self):
        a = rt.new_meeting("共用甲")
        rt.handle_message("你", "@claude 在吗")
        wait_idle()
        sid = rt.meta["claude_session"]
        proj, _ = make_claude_files(sid)
        b = rt.new_meeting("共用乙")
        rt.meta["claude_session"] = sid    # 两场会议指向同一个会话（比如复制出来的）
        rt.save_meta()
        rt.new_meeting("第三场")
        rt.trash_meeting(a)
        rt.purge_all()
        time.sleep(0.5)
        self.assertTrue(os.path.exists(os.path.join(proj, sid + ".jsonl")), "别的会议还在用，不删")

    def test_bad_sid_ignored(self):
        self.assertEqual(rt.delete_claude_session("../../x"), 0)
        self.assertEqual(rt.delete_claude_session("*"), 0)
        self.assertEqual(rt.meeting_sessions({"claude_session": "..\\..", "codex_session": "abc-def-123"}),
                         {"claude": [], "codex": ["abc-def-123"]})

    def test_partial_quote(self):
        rt.new_meeting("引用一段")
        rt.handle_message("你", "@claude 说点长的")
        wait_idle()
        cm = [m for m in rt.messages if m["speaker"] == "claude"][-1]
        calls.clear()
        rt.handle_message("你", "这个对吗", reply_to=rt.make_reply(cm["id"], "  好\n了  "))
        wait_idle()
        self.assertEqual([c[0] for c in calls], ["claude"])
        self.assertIn("（回复 [claude] 那条里的这段：“好 了”）这个对吗", calls[0][1])
        r = rt.make_reply(cm["id"], "x" * 300)
        self.assertTrue(r["part"])
        self.assertEqual(len(r["text"]), rt.QUOTE_LEN + 1)


if __name__ == "__main__":
    unittest.main()
