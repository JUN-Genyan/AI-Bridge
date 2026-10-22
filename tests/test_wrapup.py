import os, sys, tempfile, time, unittest
os.environ["ROUNDTABLE_LOG_DIR"] = tempfile.mkdtemp(prefix="rt_wrap_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import room as R, agents, common, web, workspace as ws
ROOM = None   # 这个测试文件当前在用的会议


def new_room(title, ann="", wd=None):
    global ROOM
    ROOM = R.rooms.create(title, ann, wd)
    return ROOM.id

calls = []


def fake(ai, prompt, sid, imgs, mode=None, room=None):
    calls.append((ai, mode, prompt))
    return True, "@用户 好了", sid or ("sid-" + ai), []


R.ai_caller = fake
ws.snapshot_tree = lambda wd: "tree"
R.Room.record_step = lambda self, *a, **k: None


def wait_idle():
    for _ in range(100):
        if not ROOM.busy_for():
            return
        time.sleep(0.05)


class T(unittest.TestCase):
    def setUp(self):
        calls.clear()
        new_room("wrap%d" % time.time_ns())

    def test_forced_work_then_back(self):
        self.assertEqual(ROOM.meta["mode"], "discuss")
        ok, err = ROOM.request_wrapup()
        self.assertTrue(ok, err)
        wait_idle()
        self.assertEqual([(a, m) for a, m, _ in calls], [("claude", "work")])
        self.assertIn("【落盘】", calls[0][2])
        self.assertEqual(ROOM.meta["mode"], "discuss")          # 会议本身的模式没变
        self.assertNotIn("claude", ROOM.state["force_work"])
        # 下一轮普通发言回到讨论模式，且只有 codex 看不到落盘那条
        ROOM.handle_message("你", "@claude 再说一句")
        wait_idle()
        self.assertEqual(calls[-1][:2], ("claude", "discuss"))
        ROOM.handle_message("你", "@codex 你好")
        wait_idle()
        self.assertEqual(calls[-1][0], "codex")
        self.assertNotIn("【落盘】", calls[-1][2])

    def test_refuse_when_handed_off(self):
        ROOM.handle_message("你", "@claude 在吗")
        wait_idle()
        ROOM.set_handoff("claude", True)
        ok, err = ROOM.request_wrapup()
        self.assertFalse(ok)
        self.assertIn("终端", err)
        self.assertNotIn("claude", ROOM.state["force_work"])

    def test_user_message_not_blocked_by_serial_queue(self):
        import threading
        ROOM.handle_message("你", "@claude 先建会话")
        wait_idle()
        gate = threading.Event()
        orig = ROOM.process_job
        # 串行队列被一个卡住的活占着
        ROOM.jobs.put((ROOM.id, "reply", "codex"))
        ROOM.pending.append("codex")
        hold = threading.Lock(); hold.acquire()
        real_locks = dict(ROOM.ai_locks)
        ROOM.ai_locks["codex"] = hold          # 让队列里的 codex 一直拿不到锁
        try:
            ROOM.enqueue("claude", user=False)  # claude 排在队列里、codex 后面
            n = len(calls)
            ROOM.handle_message("你", "@claude 卡了吗")
            for _ in range(100):
                if len(calls) > n:
                    break
                time.sleep(0.05)
            self.assertGreater(len(calls), n, "用户发话后 claude 应该马上并行开跑")
            self.assertEqual(calls[-1][0], "claude")
        finally:
            hold.release()
            ROOM.ai_locks.update(real_locks)
            wait_idle()

    def test_rules_change_resent(self):
        ROOM.handle_message("你", "@claude 你好")
        wait_idle()
        ROOM.meta["mode_seen"]["claude"] = "discuss"          # 旧版本存的是不带指纹的模式名
        ROOM.handle_message("你", "@claude 再来")
        wait_idle()
        self.assertIn("会议室规矩改了", calls[-1][2])
        ROOM.handle_message("你", "@claude 第三句")
        wait_idle()
        self.assertNotIn("会议室规矩改了", calls[-1][2])     # 只补一次

    def test_stop_kills_running_and_drops_queue(self):
        import subprocess, threading
        ROOM.handle_message("你", "@claude 建会话")
        ROOM.handle_message("你", "@codex 建会话")
        wait_idle()
        started = threading.Event()
        child = {}

        def slow(ai, prompt, sid, imgs, mode=None, room=None):
            # 模拟一个正在跑的 claude：起一个真子进程挂着，被杀了才返回
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            ROOM.procs[ai] = p
            child["p"] = p
            started.set()
            p.wait()
            return False, "claude 出错了（退出码 1）", sid, []

        R.ai_caller = slow
        try:
            ROOM.handle_message("你", "@claude 干个长活")
            self.assertTrue(started.wait(10))
            ROOM.enqueue("codex", user=False)          # 队列里还排着一个
            n = len(calls)
            t0 = time.time()
            msg = ROOM.stop_ais(common.AIS)
            wait_idle()
            self.assertLess(time.time() - t0, 10)
            self.assertIsNotNone(child["p"].poll(), "子进程应该被杀掉")
            self.assertIn("claude", msg)
            texts = [m["text"] for m in ROOM.messages[-3:]]
            self.assertTrue(any("被你打断了" in t for t in texts), texts)
            time.sleep(0.5)
            self.assertEqual(len(calls), n, "排队的 codex 不该再跑")
            self.assertFalse(ROOM.busy_for())
        finally:
            R.ai_caller = fake
        # 停过之后照常能用
        ROOM.handle_message("你", "@claude 还在吗")
        wait_idle()
        self.assertEqual(calls[-1][0], "claude")

    def test_flag_cleared_if_job_bails(self):
        ROOM.state["force_work"].add("claude")
        ROOM.process_job("claude", "not-this-meeting", True)   # 会议对不上，提前返回
        self.assertNotIn("claude", ROOM.state["force_work"])


if __name__ == "__main__":
    unittest.main()
