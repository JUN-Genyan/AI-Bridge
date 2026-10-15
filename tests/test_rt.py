# -*- coding: utf-8 -*-
import os, sys, json, tempfile, subprocess, threading, urllib.request, urllib.error, unittest, shutil, time

TMP = tempfile.mkdtemp(prefix="rt_test_")
os.environ["ROUNDTABLE_LOG_DIR"] = os.path.join(TMP, "rt")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import roundtable as rt
from http.server import ThreadingHTTPServer

# 假项目仓库（refs 测试用）
REPO = os.path.join(TMP, "repo")
os.makedirs(REPO)
subprocess.run(["git", "init", "-q", REPO], check=True)
rt.PROJECT = REPO

srv = ThreadingHTTPServer(("127.0.0.1", 0), rt.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def call(method, path, obj=None, auth=True, extra=None):
    h = {"Content-Type": "application/json"}
    if auth:
        h["X-RT-Token"] = rt.RT_TOKEN
    h.update(extra or {})
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), method=method,
                                 data=json.dumps(obj).encode() if obj is not None else None, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def reset():
    with rt.lock:
        rt.meta = None
        rt.mdir = None
        rt.messages.clear()
        rt.pending.clear()
        rt.state["running"].clear()
        rt.state["compacting"].clear()
    shutil.rmtree(os.environ["ROUNDTABLE_LOG_DIR"], ignore_errors=True)


def status_of(mid):
    with open(os.path.join(rt.MEET_DIR, mid, "meeting.json"), encoding="utf-8") as f:
        return json.load(f)["status"]


class T(unittest.TestCase):
    def setUp(self):
        reset()

    def test_new_keeps_old_open(self):
        a = rt.new_meeting("甲")
        b = rt.new_meeting("乙")
        self.assertEqual(status_of(a), "open")
        self.assertEqual(rt.mid_now(), b)
        self.assertTrue(rt.open_meeting(a))
        self.assertEqual(status_of(a), "open")
        self.assertEqual(status_of(b), "open")

    def test_archive(self):
        a = rt.new_meeting("甲")
        b = rt.new_meeting("乙")
        c = rt.new_meeting("丙")
        s, j = call("POST", "/api/meetings/archive", {"id": c, "on": True})
        self.assertEqual(s, 200)
        self.assertEqual(status_of(c), "archived")
        lst = rt.list_meetings()
        self.assertEqual(lst[-1]["id"], c)
        self.assertTrue(lst[-1]["archived"])
        # 老数据 closed 读作已归档
        p = os.path.join(rt.MEET_DIR, a, "meeting.json")
        m = json.load(open(p, encoding="utf-8"))
        m["status"] = "closed"
        json.dump(m, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        self.assertTrue(next(x for x in rt.list_meetings() if x["id"] == a)["archived"])
        s, j = call("POST", "/api/meetings/archive", {"id": c, "on": False})
        self.assertEqual(status_of(c), "open")
        self.assertEqual(call("POST", "/api/meetings/archive", {"id": "不存在", "on": True})[0], 409)

    def test_switch_busy_409(self):
        a = rt.new_meeting("甲")
        rt.state["running"].append("claude")
        self.assertEqual(call("POST", "/api/meetings/new", {"title": "x"})[0], 409)
        self.assertEqual(call("POST", "/api/meetings/open", {"id": a})[0], 409)
        rt.state["running"].clear()
        self.assertEqual(call("POST", "/api/meetings/new", {"title": "x"})[0], 200)

    def _fake_global(self):
        d = tempfile.mkdtemp(dir=TMP)
        cs, cc = os.path.join(d, "settings.json"), os.path.join(d, "config.toml")
        json.dump({"model": "opus", "modelSettings": {"claude-sonnet-5": {"effortLevel": "low"},
                                                      "claude-opus-4-8": {"effortLevel": "high"}}}, open(cs, "w"))
        open(cc, "w", encoding="utf-8").write('model = "gpt-x"\nmodel_reasoning_effort = "medium"\n[desktop]\nenabled-reasoning-efforts = ["low","high","max"]\n')
        rt.CLAUDE_SETTINGS, rt.CODEX_CONFIG = cs, cc

    def test_models(self):
        self._fake_global()
        rt.new_meeting("甲")
        self.assertEqual(rt.global_claude(), ("opus", "high"))
        self.assertEqual(rt.global_codex(), ("gpt-x", "medium"))
        self.assertEqual(rt.model_label("claude"), "opus · high")
        self.assertEqual(rt.model_label("codex"), "gpt-x · medium")
        self.assertEqual(rt.effort_options("codex"), ["low", "high", "max"])
        # 读不到
        rt.CLAUDE_SETTINGS = os.path.join(TMP, "none.json")
        rt.CODEX_CONFIG = os.path.join(TMP, "none.toml")
        self.assertEqual(rt.model_label("claude"), "默认 · 默认")
        self.assertEqual(rt.effort_options("codex"), ["low", "medium", "high"])
        self._fake_global()
        # 存取
        s, j = call("POST", "/api/models", {"ai": "claude", "model": "sonnet", "effort": "low"})
        self.assertEqual(s, 200)
        self.assertTrue(j["model_changed"])
        self.assertEqual(rt.model_label("claude"), "sonnet · low")
        st = call("GET", "/api/state")[1]
        self.assertEqual(st["ais"]["claude"]["model"]["label"], "sonnet · low")
        self.assertTrue(any("claude 换成了 sonnet · low" in m["text"] and m.get("quiet") for m in rt.messages))
        # 重载后还在
        rt.load_meeting(rt.mid_now())
        self.assertEqual(rt.own_models("claude"), ("sonnet", "low"))
        self.assertEqual(call("POST", "/api/models", {"ai": "codex", "model": "", "effort": "bogus"})[0], 409)
        self.assertEqual(call("POST", "/api/models", {"ai": "codex", "model": "a b;c", "effort": ""})[0], 409)
        self.assertEqual(call("POST", "/api/models", {"ai": "bob", "model": "", "effort": ""})[0], 409)
        rt.state["running"].append("codex")
        self.assertEqual(call("POST", "/api/models", {"ai": "codex", "model": "x", "effort": ""})[0], 409)
        rt.state["running"].clear()
        # 清回跟随全局
        call("POST", "/api/models", {"ai": "claude", "model": "", "effort": ""})
        self.assertEqual(rt.model_label("claude"), "opus · high")

    def test_cmd_args(self):
        for mode in ("discuss", "work"):
            c = rt.build_cmd("claude", mode, None, "claude")
            self.assertNotIn("--model", c)
            self.assertNotIn("--effort", c)
            c = rt.build_cmd("claude", mode, "sid", "claude", model="sonnet", effort="low")
            self.assertEqual(c[c.index("--model") + 1], "sonnet")
            self.assertEqual(c[c.index("--effort") + 1], "low")
            for sid in (None, "sid"):
                c = rt.build_cmd("codex", mode, sid, "codex", (), "o.txt")
                self.assertNotIn("-m", c)
                self.assertFalse(any(x.startswith("model_reasoning_effort") for x in c))
                c = rt.build_cmd("codex", mode, sid, "codex", (), "o.txt", "gpt-x", "low")
                self.assertEqual(c[c.index("-m") + 1], "gpt-x")
                self.assertIn('model_reasoning_effort="low"', c)
                self.assertEqual(c[-1], "-")

    def test_call_ai_passes_models(self):
        rt.new_meeting("甲")
        rt.meta["models"]["claude"] = {"model": "haiku", "effort": "max"}
        seen = {}
        orig = rt.run_stream
        rt.run_stream = lambda cmd, prompt, on_line=None, timeout=None, tag=None: (seen.setdefault("cmd", cmd), (0, "", "", False))[1]
        orig_which = rt.shutil.which
        rt.shutil.which = lambda n: n
        try:
            rt.call_ai("claude", "hi")
        finally:
            rt.run_stream, rt.shutil.which = orig, orig_which
        c = seen["cmd"]
        self.assertEqual(c[c.index("--model") + 1], "haiku")
        self.assertEqual(c[c.index("--effort") + 1], "max")

    def test_delete_flow(self):
        a = rt.new_meeting("甲")
        b = rt.new_meeting("乙")   # 当前
        # 当前不能删
        self.assertEqual(call("POST", "/api/meetings/delete", {"id": b})[0], 409)
        # AI 忙不能删
        rt.state["running"].append("claude")
        self.assertEqual(call("POST", "/api/meetings/delete", {"id": a})[0], 409)
        rt.state["running"].clear()
        # 造一个存档点 ref，名字按会议目录名
        subprocess.run(["git", "-C", REPO, "commit", "--allow-empty", "-q", "-m", "x"],
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                GIT_COMMITTER_EMAIL="t@t"), check=True)
        sha = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        ref = "refs/roundtable/%s/1" % rt.ref_safe(a)
        other = "refs/roundtable/%s/1" % rt.ref_safe(b)
        subprocess.run(["git", "-C", REPO, "update-ref", ref, sha], check=True)
        subprocess.run(["git", "-C", REPO, "update-ref", other, sha], check=True)
        s, j = call("POST", "/api/meetings/delete", {"id": a})
        self.assertEqual(s, 200)
        self.assertFalse(os.path.exists(os.path.join(rt.MEET_DIR, a)))
        self.assertTrue(os.path.isdir(os.path.join(rt.LOG_DIR, "trash", a)))
        self.assertEqual([x["id"] for x in call("GET", "/api/trash")[1]["trash"]], [a])
        self.assertNotIn(a, [x["id"] for x in rt.list_meetings()])
        # 恢复
        self.assertEqual(call("POST", "/api/trash/restore", {"id": a})[0], 200)
        self.assertTrue(os.path.isdir(os.path.join(rt.MEET_DIR, a)))
        self.assertEqual(call("GET", "/api/trash")[1]["trash"], [])
        # 再删，一键清空 -> 删，refs 清
        call("POST", "/api/meetings/delete", {"id": a})
        self.assertEqual(call("POST", "/api/trash/purge", {})[0], 200)
        self.assertFalse(os.path.exists(os.path.join(rt.LOG_DIR, "trash", a)))
        refs = subprocess.run(["git", "-C", REPO, "for-each-ref", "--format=%(refname)", "refs/roundtable"],
                              capture_output=True, text=True).stdout.split()
        self.assertEqual(refs, [other])   # 别的会议的不动

    def test_trash_name_collision_keeps_ref_name(self):
        a = rt.new_meeting("甲")
        rt.new_meeting("乙")
        os.makedirs(os.path.join(rt.LOG_DIR, "trash", a))   # 占位（无 meeting.json）
        self.assertEqual(rt.trash_meeting(a), (True, ""))
        names = [x["id"] for x in rt.list_trash()]
        self.assertEqual(names, [a + "_2"])
        self.assertEqual(json.load(open(os.path.join(rt.LOG_DIR, "trash", a + "_2", "meeting.json"), encoding="utf-8"))["ref_name"], rt.ref_safe(a))

    def test_path_traversal(self):
        a = rt.new_meeting("甲")
        rt.new_meeting("乙")
        for bad in ("..", "../..", "..\\..", a + "/..", "/etc", "C:\\Windows", "files", ""):
            self.assertEqual(call("POST", "/api/meetings/delete", {"id": bad})[0], 409, bad)
            self.assertEqual(call("POST", "/api/trash/restore", {"id": bad})[0], 409, bad)
        self.assertTrue(os.path.isdir(os.path.join(rt.MEET_DIR, a)))
        # 回收站里没有 meeting.json 的目录，清空时不碰
        os.makedirs(os.path.join(rt.LOG_DIR, "trash", "junk"))
        self.assertEqual(call("POST", "/api/trash/purge", {})[0], 200)
        self.assertTrue(os.path.isdir(os.path.join(rt.LOG_DIR, "trash", "junk")))

    def test_identity(self):
        a = rt.new_meeting("甲")
        rt.new_meeting("乙")
        for path, body in (("/api/meetings/archive", {"id": a, "on": True}), ("/api/meetings/delete", {"id": a}),
                           ("/api/trash/restore", {"id": a}), ("/api/trash/purge", {}),
                           ("/api/models", {"ai": "claude", "model": "x", "effort": ""}),
                           ("/api/send", {"text": "hi"}), ("/api/meetings/new", {"title": "x"})):
            self.assertEqual(call("POST", path, body, auth=False)[0], 403, path)
        self.assertEqual(status_of(a), "open")
        self.assertEqual(rt.own_models("claude"), ("", ""))
        # same-origin 头可以
        self.assertEqual(call("POST", "/api/models", {"ai": "claude", "model": "x", "effort": ""}, auth=False,
                              extra={"Sec-Fetch-Site": "same-origin"})[0], 200)
        # /api/post 只收 claude / codex
        self.assertEqual(call("POST", "/api/post", {"speaker": "你", "text": "x"}, auth=False)[0], 400)
        self.assertEqual(call("POST", "/api/post", {"speaker": "系统", "text": "x"}, auth=False)[0], 400)


if __name__ == "__main__":
    try:
        unittest.main(exit=False, verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
