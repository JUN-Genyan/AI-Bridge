# -*- coding: utf-8 -*-
"""会议目录的增删、归档、回收站；模型设置；命令行；身份规则。"""
import os, sys, json, tempfile, subprocess, threading, urllib.request, urllib.error, unittest, shutil, time
from urllib.parse import quote

TMP = tempfile.mkdtemp(prefix="rt_test_")
os.environ["ROUNDTABLE_LOG_DIR"] = os.path.join(TMP, "rt")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import room as R, agents, common, web

# 假项目仓库（refs 测试用）
REPO = os.path.join(TMP, "repo")
os.makedirs(REPO)
subprocess.run(["git", "init", "-q", REPO], check=True)

srv = web.ExclusiveServer(("127.0.0.1", 0), web.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
R.ai_caller = lambda ai, prompt, sid, imgs, mode=None, room=None: (True, "@用户 好", sid or "s-" + ai, [])


def call(method, path, obj=None, auth=True, extra=None):
    h = {"Content-Type": "application/json"}
    if auth:
        h["X-RT-Token"] = web.RT_TOKEN
    h.update(extra or {})
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), method=method,
                                 data=json.dumps(obj).encode() if obj is not None else None, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def rp(r, act):
    return "/api/r/%s/%s" % (quote(r.id), act)


def new(title):
    return R.rooms.create(title, "", REPO)


def reset():
    for r in R.rooms.all():
        R.rooms.unload(r.id)
    shutil.rmtree(common.MEET_DIR, ignore_errors=True)
    shutil.rmtree(common.TRASH_DIR, ignore_errors=True)


def status_of(mid):
    with open(os.path.join(common.MEET_DIR, mid, "meeting.json"), encoding="utf-8") as f:
        return json.load(f)["status"]


class T(unittest.TestCase):
    def setUp(self):
        reset()

    def test_new_keeps_old_open(self):
        a = new("甲")
        b = new("乙")
        self.assertEqual(status_of(a.id), "open")
        self.assertEqual(status_of(b.id), "open")
        self.assertEqual(R.last_room(), b.id, "最近开的那场")
        self.assertIs(R.rooms.get(a.id), a, "两场都在内存里，各跑各的")

    def test_archive(self):
        a, b, c = new("甲"), new("乙"), new("丙")
        s, j = call("POST", "/api/rooms/archive", {"id": c.id, "on": True})
        self.assertEqual(s, 200)
        self.assertEqual(status_of(c.id), "archived")
        lst = R.rooms.listing()
        self.assertEqual(lst[-1]["id"], c.id)
        self.assertTrue(lst[-1]["archived"])
        # 老数据 closed 读作已归档（没读进内存的会议从磁盘读）
        R.rooms.unload(a.id)
        p = os.path.join(common.MEET_DIR, a.id, "meeting.json")
        m = json.load(open(p, encoding="utf-8"))
        m["status"] = "closed"
        json.dump(m, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        self.assertTrue(next(x for x in R.rooms.listing() if x["id"] == a.id)["archived"])
        call("POST", "/api/rooms/archive", {"id": c.id, "on": False})
        self.assertEqual(status_of(c.id), "open")
        self.assertEqual(call("POST", "/api/rooms/archive", {"id": "不存在", "on": True})[0], 409)

    def test_busy_room_does_not_block_others(self):
        a = new("甲")
        a.state["running"].append("claude")
        self.assertEqual(call("POST", "/api/rooms/new", {"title": "x", "workdir": REPO})[0], 200, "别的会议在跑也能开新的")
        self.assertEqual(call("POST", "/api/rooms/delete", {"id": a.id})[0], 409, "在跑的那场不能删")
        a.state["running"].clear()

    def _fake_global(self):
        d = tempfile.mkdtemp(dir=TMP)
        cs, cc = os.path.join(d, "settings.json"), os.path.join(d, "config.toml")
        json.dump({"model": "opus", "modelSettings": {"claude-sonnet-5": {"effortLevel": "low"},
                                                      "claude-opus-4-8": {"effortLevel": "high"}}}, open(cs, "w"))
        open(cc, "w", encoding="utf-8").write('model = "gpt-x"\nmodel_reasoning_effort = "medium"\n[desktop]\nenabled-reasoning-efforts = ["low","high","max"]\n')
        agents.CLAUDE_SETTINGS, agents.CODEX_CONFIG = cs, cc

    def test_models(self):
        self._fake_global()
        r = new("甲")
        self.assertEqual(agents.global_claude(), ("opus", "high"))
        self.assertEqual(agents.global_codex(), ("gpt-x", "medium"))
        # 没设用会议室默认，不跟全局
        self.assertEqual(r.model_label("claude"), "claude-opus-5-5 · medium")
        self.assertEqual(r.model_label("codex"), "gpt-6.1-sol · medium")
        self.assertIn("claude-opus-5-5", agents.model_options("claude"))
        # 存取
        s, j = call("POST", rp(r, "models"), {"ai": "claude", "model": "sonnet", "effort": "low"})
        self.assertEqual(s, 200)
        self.assertTrue(j["model_changed"])
        self.assertEqual(r.model_label("claude"), "sonnet · low")
        st = call("GET", rp(r, "state"))[1]
        self.assertEqual(st["ais"]["claude"]["model"]["label"], "sonnet · low")
        self.assertTrue(any("claude 换成了 sonnet · low" in m["text"] and m.get("quiet") for m in r.messages))
        # 重载后还在
        R.rooms.unload(r.id)
        r = R.rooms.get(r.id)
        self.assertEqual(r.own_models("claude"), ("sonnet", "low"))
        self.assertEqual(call("POST", rp(r, "models"), {"ai": "codex", "model": "", "effort": "bogus"})[0], 409)
        self.assertEqual(call("POST", rp(r, "models"), {"ai": "codex", "model": "a b;c", "effort": ""})[0], 409)
        self.assertEqual(call("POST", rp(r, "models"), {"ai": "bob", "model": "", "effort": ""})[0], 409)
        r.state["running"].append("codex")
        self.assertEqual(call("POST", rp(r, "models"), {"ai": "codex", "model": "x", "effort": ""})[0], 409)
        r.state["running"].clear()
        # 清回默认
        call("POST", rp(r, "models"), {"ai": "claude", "model": "", "effort": ""})
        self.assertEqual(r.model_label("claude"), "claude-opus-5-5 · medium")

    def test_cmd_args(self):
        for mode in ("discuss", "work"):
            c = agents.build_cmd("claude", mode, None, "claude")
            self.assertNotIn("--model", c)
            self.assertNotIn("--effort", c)
            c = agents.build_cmd("claude", mode, "sid", "claude", model="sonnet", effort="low")
            self.assertEqual(c[c.index("--model") + 1], "sonnet")
            self.assertEqual(c[c.index("--effort") + 1], "low")
            st = json.loads(c[c.index("--settings") + 1])
            self.assertNotIn("disableAllHooks", st, "会把护栏钩子一起关掉")
            self.assertEqual(st["hooks"]["PreToolUse"][0]["hooks"][0]["type"], "http")
            mc = json.loads(c[c.index("--mcp-config") + 1])
            self.assertEqual(list(mc["mcpServers"]), ["bridge"])
            for sid in (None, "sid"):
                c = agents.build_cmd("codex", mode, sid, "codex", (), "o.txt")
                self.assertNotIn("-m", c)
                self.assertFalse(any(x.startswith("model_reasoning_effort") for x in c))
                c = agents.build_cmd("codex", mode, sid, "codex", (), "o.txt", "gpt-x", "low")
                self.assertEqual(c[c.index("-m") + 1], "gpt-x")
                self.assertIn('model_reasoning_effort="low"', c)
                self.assertEqual(c[-1], "-")
        # exe 可以是"真身 + 脚本"
        self.assertEqual(agents.build_cmd("claude", "discuss", None, ["node", "cli.js"])[:3], ["node", "cli.js", "-p"])

    def test_call_ai_passes_models(self):
        r = new("甲")
        r.meta["models"]["claude"] = {"model": "haiku", "effort": "max"}
        seen = {}
        orig_rs, orig_rc = agents.run_stream, agents.resolve_cmd

        def fake_rs(cmd, prompt, on_line=None, timeout=None, cwd=None, env=None, reg=None):
            seen["cmd"], seen["env"], seen["cwd"] = cmd, env, cwd
            return 0, "", "", None

        agents.run_stream, agents.resolve_cmd = fake_rs, lambda n: [n]
        try:
            agents.call_ai("claude", "hi", None, (), None, r)
            c = seen["cmd"]
            self.assertEqual(c[c.index("--model") + 1], "haiku")
            self.assertEqual(c[c.index("--effort") + 1], "max")
            self.assertEqual(seen["cwd"], REPO)
            self.assertEqual(seen["env"]["AIBRIDGE_KEY"], r.keys["claude"])
            self.assertIn(r.keys["claude"], c[c.index("--mcp-config") + 1], "bridge 带这场会议的钥匙")
            # 没设：明着传会议室默认
            r.meta["models"]["claude"] = {"model": "", "effort": ""}
            agents.call_ai("claude", "hi", None, (), None, r)
            c = seen["cmd"]
            self.assertEqual(c[c.index("--model") + 1], "claude-opus-5-5")
            self.assertEqual(c[c.index("--effort") + 1], "medium")
            # 会话名 = 会议名，同一会话名字没变就不重复设
            self.assertEqual(c[c.index("-n") + 1], "甲")
            sid = c[c.index("--session-id") + 1]
            agents.call_ai("claude", "hi", sid, (), None, r)
            self.assertNotIn("-n", seen["cmd"])
        finally:
            agents.run_stream, agents.resolve_cmd = orig_rs, orig_rc

    def test_delete_flow(self):
        a = new("甲")
        b = new("乙")
        # 在跑的不能删
        a.state["running"].append("claude")
        self.assertEqual(call("POST", "/api/rooms/delete", {"id": a.id})[0], 409)
        a.state["running"].clear()
        # 造一个存档点 ref，名字按会议目录名
        subprocess.run(["git", "-C", REPO, "commit", "--allow-empty", "-q", "-m", "x"],
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                GIT_COMMITTER_EMAIL="t@t"), check=True)
        sha = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        ref = "refs/roundtable/%s/1" % common.ref_safe(a.id)
        other = "refs/roundtable/%s/1" % common.ref_safe(b.id)
        subprocess.run(["git", "-C", REPO, "update-ref", ref, sha], check=True)
        subprocess.run(["git", "-C", REPO, "update-ref", other, sha], check=True)
        s, j = call("POST", "/api/rooms/delete", {"id": a.id})
        self.assertEqual(s, 200)
        self.assertNotIn(a.id, R.rooms.loaded, "删了的会议不留在内存里")
        self.assertTrue(a.closed)
        self.assertFalse(os.path.exists(os.path.join(common.MEET_DIR, a.id)))
        self.assertTrue(os.path.isdir(os.path.join(common.TRASH_DIR, a.id)))
        self.assertEqual([x["id"] for x in call("GET", "/api/trash")[1]["trash"]], [a.id])
        self.assertNotIn(a.id, [x["id"] for x in R.rooms.listing()])
        self.assertEqual(call("GET", rp(a, "state"))[0], 404)
        # 恢复
        self.assertEqual(call("POST", "/api/trash/restore", {"id": a.id})[0], 200)
        self.assertTrue(os.path.isdir(os.path.join(common.MEET_DIR, a.id)))
        self.assertEqual(call("GET", "/api/trash")[1]["trash"], [])
        self.assertEqual(call("GET", rp(a, "state"))[0], 200, "恢复后又能打开")
        # 再删，一键清空 -> 删，refs 清
        call("POST", "/api/rooms/delete", {"id": a.id})
        self.assertEqual(call("POST", "/api/trash/purge", {})[0], 200)
        self.assertFalse(os.path.exists(os.path.join(common.TRASH_DIR, a.id)))
        refs = subprocess.run(["git", "-C", REPO, "for-each-ref", "--format=%(refname)", "refs/roundtable"],
                              capture_output=True, text=True).stdout.split()
        self.assertEqual(refs, [other])   # 别的会议的不动

    def test_trash_name_collision_keeps_ref_name(self):
        a = new("甲")
        new("乙")
        os.makedirs(os.path.join(common.TRASH_DIR, a.id))   # 占位（无 meeting.json）
        self.assertEqual(R.trash_meeting(a.id), (True, ""))
        names = [x["id"] for x in R.list_trash()]
        self.assertEqual(names, [a.id + "_2"])
        self.assertEqual(json.load(open(os.path.join(common.TRASH_DIR, a.id + "_2", "meeting.json"), encoding="utf-8"))["ref_name"], common.ref_safe(a.id))

    def test_path_traversal(self):
        a = new("甲")
        new("乙")
        for bad in ("..", "../..", "..\\..", a.id + "/..", "/etc", "C:\\Windows", "files", ""):
            self.assertEqual(call("POST", "/api/rooms/delete", {"id": bad})[0], 409, bad)
            self.assertEqual(call("POST", "/api/trash/restore", {"id": bad})[0], 409, bad)
            self.assertIsNone(R.rooms.get(bad), bad)
        self.assertTrue(os.path.isdir(os.path.join(common.MEET_DIR, a.id)))
        self.assertEqual(call("GET", "/api/r/%s/state" % quote("..", safe=""))[0], 404)
        # 回收站里没有 meeting.json 的目录，清空时不碰
        os.makedirs(os.path.join(common.TRASH_DIR, "junk"))
        self.assertEqual(call("POST", "/api/trash/purge", {})[0], 200)
        self.assertTrue(os.path.isdir(os.path.join(common.TRASH_DIR, "junk")))

    def test_identity(self):
        a = new("甲")
        for path, body in (("/api/rooms/archive", {"id": a.id, "on": True}), ("/api/rooms/delete", {"id": a.id}),
                           ("/api/trash/restore", {"id": a.id}), ("/api/trash/purge", {}),
                           (rp(a, "models"), {"ai": "claude", "model": "x", "effort": ""}),
                           (rp(a, "send"), {"text": "hi"}), ("/api/rooms/new", {"title": "x"}),
                           ("/api/claims/release", {"key": "x"})):
            self.assertEqual(call("POST", path, body, auth=False)[0], 403, path)
        self.assertEqual(status_of(a.id), "open")
        self.assertEqual(a.own_models("claude"), ("", ""))
        # same-origin 头可以
        self.assertEqual(call("POST", rp(a, "models"), {"ai": "claude", "model": "x", "effort": ""}, auth=False,
                              extra={"Sec-Fetch-Site": "same-origin"})[0], 200)
        # /api/post 只收 claude / codex
        self.assertEqual(call("POST", "/api/post", {"speaker": "你", "text": "x"}, auth=False)[0], 400)
        self.assertEqual(call("POST", "/api/post", {"speaker": "系统", "text": "x"}, auth=False)[0], 400)

    def test_tools_config(self):
        r = new("工具")
        self.assertEqual(r.meta["tools"]["claude"], {"mcp": [], "plugins": {}, "skills_off": []})
        p = {"id": "superpowers@x", "enabled": True, "hooks": True, "synced": False}
        q = {"id": "plain@x", "enabled": True, "hooks": False, "synced": False}
        s = {"id": "slack@synced", "enabled": True, "hooks": False, "synced": True}
        cfg = r.meta["tools"]["claude"]
        self.assertEqual([agents.plugin_on(x, cfg) for x in (p, q, s)], [False, True, False], "带钩子的、同步来的默认关")
        ok, _ = r.set_tools("claude", {"mcp": ["blender"], "plugins": {"superpowers@x": True}, "skills_off": ["comfy", "坏 名字!"]})
        self.assertTrue(ok)
        cfg = r.meta["tools"]["claude"]
        self.assertEqual(cfg, {"mcp": ["blender"], "plugins": {"superpowers@x": True}, "skills_off": ["comfy"]})
        self.assertTrue(agents.plugin_on(p, cfg))
        r.state["running"].append("claude")
        self.assertEqual(r.set_tools("claude", {"mcp": []})[0], False, "在跑时不能改")
        r.state["running"].clear()
        # 讨论模式不加载勾上的 MCP，干活模式加载
        agents.claude_mcp_defs = lambda wd: {"blender": ({"command": "blender-mcp"}, "用户级")}
        self.assertEqual(list(json.loads(agents.claude_mcp_config(r, "discuss", "k"))["mcpServers"]), ["bridge"])
        self.assertEqual(list(json.loads(agents.claude_mcp_config(r, "work", "k"))["mcpServers"]), ["bridge", "blender"])


if __name__ == "__main__":
    try:
        unittest.main(exit=False, verbosity=2)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
