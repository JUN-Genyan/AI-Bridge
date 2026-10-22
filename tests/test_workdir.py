import os, sys, tempfile, time, unittest, subprocess
os.environ["ROUNDTABLE_LOG_DIR"] = tempfile.mkdtemp(prefix="rt_wd_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import room as R, agents, common, web, workspace as ws
ROOM = None   # 这个测试文件当前在用的会议


def new_room(title, ann="", wd=None):
    global ROOM
    ROOM = R.rooms.create(title, ann, wd)
    return ROOM.id

calls = []


def fake(ai, prompt, sid, imgs, mode=None, room=None):
    calls.append((ai, mode, prompt, ROOM.workdir()))
    return True, "@用户 好了", sid or ("sid-" + ai), []


R.ai_caller = fake


def wait_idle():
    for _ in range(100):
        if not ROOM.busy_for():
            return
        time.sleep(0.05)


class T(unittest.TestCase):
    def setUp(self):
        calls.clear()

    def test_default_is_project(self):
        new_room("d%d" % time.time_ns())
        self.assertEqual(ROOM.workdir(), common.PROJECT)
        self.assertEqual(ROOM.api_state()["workdir"], common.PROJECT)

    def test_old_meeting_without_field(self):
        m = R.fix_meta({"title": "old"})
        self.assertEqual(m["workdir"], common.PROJECT)

    def test_other_folder_prompt_and_cmd(self):
        d = tempfile.mkdtemp(prefix="rt_other_")
        new_room("o%d" % time.time_ns(), "", d)
        self.assertEqual(ROOM.workdir(), d)
        ROOM.handle_message("你", "@claude 你好")
        wait_idle()
        self.assertIn("工作目录是 %s" % d, calls[-1][2])
        self.assertEqual(calls[-1][3], d)
        cmd = agents.build_cmd("claude", "discuss", None, "claude", room=ROOM)
        self.assertIn("--add-dir", cmd)
        self.assertIn(common.LOG_DIR, cmd)
        self.assertIn(d, ROOM.handoff_cmd("claude"))
        self.assertEqual([m for m in R.rooms.listing() if m["id"] == ROOM.id][0]["workdir"], d)

    def test_non_git_work_mode_runs_and_warns_once(self):
        d = tempfile.mkdtemp(prefix="rt_nogit_")
        new_room("n%d" % time.time_ns(), "", d)
        self.assertFalse(ws.in_git(d))
        ROOM.set_mode("work")
        ROOM.handle_message("你", "@claude 干活")
        wait_idle()
        ROOM.handle_message("你", "@claude 再干")
        wait_idle()
        self.assertEqual([c[1] for c in calls], ["work", "work"])
        warns = [m for m in ROOM.messages if "不是 git 仓库" in m["text"]]
        self.assertEqual(len(warns), 1)

    def test_wrapup_generic_outside_project(self):
        d = tempfile.mkdtemp(prefix="rt_wrap_")
        new_room("w%d" % time.time_ns(), "", d)
        t = ROOM.wrapup_text()
        self.assertIn(d, t)
        self.assertNotIn("SKILL.md", t)
        d2 = tempfile.mkdtemp(prefix="rt_skill_")
        os.makedirs(os.path.join(d2, ".claude", "skills", "wrapup"))
        open(os.path.join(d2, ".claude", "skills", "wrapup", "SKILL.md"), "w").close()
        new_room("w2%d" % time.time_ns(), "", d2)
        self.assertIn("SKILL.md", ROOM.wrapup_text())

    def test_git_repo_elsewhere_snapshot(self):
        d = tempfile.mkdtemp(prefix="rt_repo_")
        subprocess.run(["git", "init", "-q", d], check=True)
        with open(os.path.join(d, "a.txt"), "w") as f:
            f.write("x")
        new_room("g%d" % time.time_ns(), "", d)
        self.assertTrue(ws.in_git(d))
        tree = ws.snapshot_tree(d)
        self.assertTrue(tree)
        top, _ = ws.git_dirs(d)
        self.assertTrue(os.path.basename(ws.snapshot_index(top)).startswith("snapshot-"))
        self.assertNotEqual(ws.snapshot_index(top), ws.snapshot_index(tempfile.mkdtemp()))   # 每个仓库一份


if __name__ == "__main__":
    unittest.main()
