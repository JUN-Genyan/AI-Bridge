import os, sys, tempfile, time, unittest, subprocess
os.environ["ROUNDTABLE_LOG_DIR"] = tempfile.mkdtemp(prefix="rt_wd_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import roundtable as rt

calls = []


def fake(ai, prompt, sid, imgs, mode=None):
    calls.append((ai, mode, prompt, rt.workdir()))
    return True, "@用户 好了", sid or ("sid-" + ai), []


rt.ai_caller = fake


def wait_idle():
    for _ in range(100):
        if not rt.busy_for():
            return
        time.sleep(0.05)


class T(unittest.TestCase):
    def setUp(self):
        calls.clear()

    def test_default_is_project(self):
        rt.new_meeting("d%d" % time.time_ns())
        self.assertEqual(rt.workdir(), rt.PROJECT)
        self.assertEqual(rt.api_state()["workdir"], rt.PROJECT)

    def test_old_meeting_without_field(self):
        m = rt.fix_meta({"title": "old"})
        self.assertEqual(m["workdir"], rt.PROJECT)

    def test_other_folder_prompt_and_cmd(self):
        d = tempfile.mkdtemp(prefix="rt_other_")
        rt.new_meeting("o%d" % time.time_ns(), "", d)
        self.assertEqual(rt.workdir(), d)
        rt.handle_message("你", "@claude 你好")
        wait_idle()
        self.assertIn("工作目录是 %s" % d, calls[-1][2])
        self.assertEqual(calls[-1][3], d)
        cmd = rt.build_cmd("claude", "discuss", None, "claude")
        self.assertIn("--add-dir", cmd)
        self.assertIn(rt.LOG_DIR, cmd)
        self.assertIn(d, rt.handoff_cmd("claude"))
        self.assertIn([m for m in rt.list_meetings() if m["current"]][0]["workdir"], (d,))

    def test_non_git_work_mode_runs_and_warns_once(self):
        d = tempfile.mkdtemp(prefix="rt_nogit_")
        rt.new_meeting("n%d" % time.time_ns(), "", d)
        self.assertFalse(rt.in_git())
        rt.set_mode("work")
        rt.handle_message("你", "@claude 干活")
        wait_idle()
        rt.handle_message("你", "@claude 再干")
        wait_idle()
        self.assertEqual([c[1] for c in calls], ["work", "work"])
        warns = [m for m in rt.messages if "不是 git 仓库" in m["text"]]
        self.assertEqual(len(warns), 1)

    def test_wrapup_generic_outside_project(self):
        d = tempfile.mkdtemp(prefix="rt_wrap_")
        rt.new_meeting("w%d" % time.time_ns(), "", d)
        t = rt.wrapup_text()
        self.assertIn(d, t)
        self.assertNotIn("SKILL.md", t)
        d2 = tempfile.mkdtemp(prefix="rt_skill_")
        os.makedirs(os.path.join(d2, ".claude", "skills", "wrapup"))
        open(os.path.join(d2, ".claude", "skills", "wrapup", "SKILL.md"), "w").close()
        rt.new_meeting("w2%d" % time.time_ns(), "", d2)
        self.assertIn("SKILL.md", rt.wrapup_text())

    def test_git_repo_elsewhere_snapshot(self):
        d = tempfile.mkdtemp(prefix="rt_repo_")
        subprocess.run(["git", "init", "-q", d], check=True)
        with open(os.path.join(d, "a.txt"), "w") as f:
            f.write("x")
        rt.new_meeting("g%d" % time.time_ns(), "", d)
        self.assertTrue(rt.in_git())
        tree = rt.snapshot_tree()
        self.assertTrue(tree)
        top, _ = rt.git_dirs()
        self.assertTrue(os.path.basename(rt.snapshot_index(top)).startswith("snapshot-"))
        self.assertNotEqual(rt.snapshot_index(top), rt.snapshot_index(tempfile.mkdtemp()))   # 每个仓库一份


if __name__ == "__main__":
    unittest.main()
