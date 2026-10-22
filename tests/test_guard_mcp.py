# -*- coding: utf-8 -*-
"""文件护栏（谁占着哪个文件，跨会议）、claude 编辑前钩子来问的接口、会议室自己的 MCP 服务。"""
import json, os, sys, tempfile, threading, time, unittest, urllib.error, urllib.request

os.environ["ROUNDTABLE_LOG_DIR"] = tempfile.mkdtemp(prefix="rt_guard_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import room as R, web, guard as G, agents
from bridge.guard import FileGuard, guard

WD = tempfile.mkdtemp(prefix="rt_guard_wd_")
for n in ("a.txt", "b.txt"):
    open(os.path.join(WD, n), "w").write("x")
os.makedirs(os.path.join(WD, "assets"))
open(os.path.join(WD, "assets", "c.png"), "w").write("x")
F = lambda n: os.path.join(WD, n)

R.ai_caller = lambda ai, prompt, sid, imgs, mode=None, room=None: (True, "@用户 好", sid or "s-" + ai, [])
srv = web.ExclusiveServer(("127.0.0.1", 0), web.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def raw(method, path, obj=None, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), method=method,
                                 data=json.dumps(obj).encode() if obj is not None else None, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            body = r.read().decode()
            return r.status, json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return e.code, json.loads(body) if body else None


class Guard(unittest.TestCase):
    def setUp(self):
        self.g = FileGuard(os.path.join(tempfile.mkdtemp(), "claims.json"))

    def test_auto_lease_and_turn_release(self):
        A, B = ("会议1", "claude"), ("会议2", "codex")
        self.assertEqual(self.g.touch(A, F("a.txt"), "一")[0], True)
        self.assertEqual(self.g.touch(A, F("a.txt"), "一")[0], True, "自己不挡自己")
        ok, hit = self.g.touch(B, F("a.txt"), "二")
        self.assertFalse(ok)
        self.assertEqual((hit["room"], hit["ai"], hit["kind"]), ("会议1", "claude", "auto"))
        self.assertTrue(self.g.touch(B, F("b.txt"), "二")[0], "别的文件不受影响")
        self.assertEqual(self.g.release_turn(A), 1)
        self.assertTrue(self.g.touch(B, F("a.txt"), "二")[0], "说完放开了")

    def test_same_room_other_ai_blocked(self):
        self.assertTrue(self.g.touch(("会议1", "claude"), F("a.txt"))[0])
        self.assertFalse(self.g.touch(("会议1", "codex"), F("a.txt"))[0], "同一场会议的另一个 AI 也挡")

    def test_claim_dir_and_expiry(self):
        A, B = ("会议1", "codex"), ("会议2", "claude")
        res = self.g.claim(A, [os.path.join(WD, "assets")], 30, "换图")
        self.assertTrue(res[0][1])
        ok, hit = self.g.touch(B, F(os.path.join("assets", "c.png")))
        self.assertFalse(ok, "占了文件夹，里面的文件都挡")
        self.assertEqual(hit["reason"], "换图")
        self.assertIn("换图", G.block_text(F("assets/c.png"), hit))
        self.assertNotIn("@", G.block_text(F("assets/c.png"), hit), "原话里别有 @codex，AI 贴出来会误叫人")
        # 明着占的不随一轮结束放开
        self.g.release_turn(A)
        self.assertFalse(self.g.touch(B, F("assets/c.png"))[0])
        # 过期
        for it in self.g.items.values():
            it["until"] = time.time() - 1
        self.assertTrue(self.g.touch(B, F("assets/c.png"))[0])

    def test_claim_conflict_and_release(self):
        A, B = ("会议1", "claude"), ("会议2", "codex")
        self.g.touch(A, F("a.txt"))
        res = self.g.claim(B, [F("a.txt"), F("b.txt")], 10, "")
        self.assertEqual([r[1] for r in res], [False, True])
        self.assertEqual(self.g.release(B), 1)
        self.assertEqual(self.g.release(A, [F("a.txt")]), 1)
        self.assertEqual(self.g.listing(), [])

    def test_user_claim_blocks_everyone(self):
        self.g.claim(("", G.USER), [F("a.txt")], 60, "我在改")
        ok, hit = self.g.touch(("会议1", "claude"), F("a.txt"))
        self.assertFalse(ok)
        self.assertIn("用户", G.block_text(F("a.txt"), hit))

    def test_claims_survive_restart(self):
        p = os.path.join(tempfile.mkdtemp(), "claims.json")
        g1 = FileGuard(p)
        g1.claim(("会议1", "claude"), [F("a.txt")], 30, "持久")
        g1.touch(("会议1", "claude"), F("b.txt"))   # 自动的不存
        g2 = FileGuard(p)
        self.assertEqual([c["reason"] for c in g2.listing()], ["持久"])

    def test_auto_lease_idle_expiry(self):
        A, B = ("会议1", "claude"), ("会议2", "codex")
        self.g.touch(A, F("a.txt"))
        for it in self.g.items.values():
            it["last"] = time.time() - G.AUTO_IDLE - 1
        self.assertTrue(self.g.touch(B, F("a.txt"))[0], "一轮里很久没碰的自动放开")


class HookEndpoint(unittest.TestCase):
    def setUp(self):
        guard.items.clear()   # 全局护栏：每个测试从空开始

    def test_claude_hook_deny_and_allow(self):
        r = R.rooms.create("钩子", "", WD)
        other = R.rooms.create("别的", "", WD)
        guard.claim((other.id, "codex"), [F("a.txt")], 30, "重写中", other.title())
        key = r.keys["claude"]
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": WD,
                   "tool_input": {"file_path": F("a.txt"), "old_string": "x", "new_string": "y"}}
        s, j = raw("POST", "/api/guard/claude", payload, {"Authorization": "Bearer " + key})
        self.assertEqual(s, 200)
        out = j["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("重写中", out["permissionDecisionReason"])
        self.assertTrue(any("文件护栏挡下了 claude" in m["text"] for m in r.messages))
        # 没人占的：放行（空决定），并且替它占上
        payload["tool_input"]["file_path"] = F("b.txt")
        s, j = raw("POST", "/api/guard/claude", payload, {"Authorization": "Bearer " + key})
        self.assertEqual((s, j), (200, {}))
        self.assertTrue(any(c["shown"] == os.path.normpath(F("b.txt")) and c["ai"] == "claude" for c in guard.listing()))
        # 相对路径按 cwd 算
        guard.release_turn((r.id, "claude"))
        payload["tool_input"]["file_path"] = "a.txt"
        s, j = raw("POST", "/api/guard/claude", payload, {"Authorization": "Bearer " + key})
        self.assertEqual(j["hookSpecificOutput"]["permissionDecision"], "deny")
        # 钥匙不对：放行（不该把 AI 卡死）
        s, j = raw("POST", "/api/guard/claude", payload, {"Authorization": "Bearer wrong-key"})
        self.assertEqual((s, j), (200, {}))
        guard.release((other.id, "codex"))

    def test_codex_hook_patch(self):
        r = R.rooms.create("codex钩子", "", WD)
        guard.claim(("别的会议", "claude"), [F("a.txt")], 30, "在重写", "别的会议")
        key = r.keys["codex"]
        patch = "*** Begin Patch\n*** Update File: b.txt\n@@\n-x\n+y\n*** Update File: %s\n@@\n-x\n+y\n*** End Patch" % F("a.txt").replace("\\", "/")
        payload = {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "cwd": WD, "tool_input": {"command": patch}}
        s, j = raw("POST", "/api/guard/codex", payload, {"Authorization": "Bearer " + key})
        self.assertEqual(j["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("在重写", j["hookSpecificOutput"]["permissionDecisionReason"])
        # 只改没人占的：放行
        payload["tool_input"]["command"] = "*** Begin Patch\n*** Add File: new.md\n+hi\n*** End Patch"
        s, j = raw("POST", "/api/guard/codex", payload, {"Authorization": "Bearer " + key})
        self.assertEqual(j, {})
        self.assertTrue(any(c["shown"] == os.path.normpath(F("new.md")) and c["ai"] == "codex" for c in guard.listing()))
        self.assertEqual(agents.patch_paths("*** Delete File: x/y.txt\n*** Move to: z.txt", "C:/w"),
                         [os.path.normpath("C:/w/x/y.txt"), os.path.normpath("C:/w/z.txt")])

    def test_turn_end_releases(self):
        r = R.rooms.create("一轮", "", WD)
        r.meta["mode"] = "work"

        def fake(ai, prompt, sid, imgs, mode=None, room=None):
            ok, hit = guard.touch((room.id, ai), F("b.txt"), room.title())
            assert ok
            assert any(c["room"] == room.id for c in guard.listing())
            return True, "@用户 改好了", sid or "s", []

        R.ai_caller = fake
        old_snap = R.ws.snapshot_tree
        R.ws.snapshot_tree = lambda wd: "tree"
        old_rec = R.Room.record_step
        R.Room.record_step = lambda self, *a, **k: None
        try:
            r.handle_message("你", "@claude 改")
            end = time.time() + 5
            while r.busy_for() and time.time() < end:
                time.sleep(0.05)
        finally:
            R.ws.snapshot_tree, R.Room.record_step = old_snap, old_rec
            R.ai_caller = lambda ai, prompt, sid, imgs, mode=None, room=None: (True, "@用户 好", sid or "s-" + ai, [])
        self.assertFalse(any(c["room"] == r.id for c in guard.listing()), "一轮说完自动占的放开了")

    def test_user_release_and_mine(self):
        r = R.rooms.create("放开", "", WD)
        guard.touch((r.id, "codex"), F("a.txt"), r.title())
        key = next(c["key"] for c in guard.listing() if c["room"] == r.id)
        tok = {"X-RT-Token": web.RT_TOKEN}
        self.assertEqual(raw("POST", "/api/claims/release", {"key": key})[0], 403, "只认网页上的用户")
        self.assertEqual(raw("POST", "/api/claims/release", {"key": key}, tok)[0], 200)
        self.assertTrue(any("你放开了 codex" in m["text"] for m in r.messages))
        s, j = raw("POST", "/api/claims/mine", {"paths": [F("a.txt")], "minutes": 5}, tok)
        self.assertEqual(s, 200)
        self.assertEqual(j["claimed"], [os.path.normpath(F("a.txt"))])
        self.assertFalse(guard.touch((r.id, "claude"), F("a.txt"))[0])
        guard.release(("", G.USER))


class MCP(unittest.TestCase):
    def setUp(self):
        guard.items.clear()

    def rpc(self, method, params=None, key=None, mid=1):
        h = {"Authorization": "Bearer " + key} if key else {}
        msg = {"jsonrpc": "2.0", "method": method}
        if mid is not None:
            msg["id"] = mid
        if params is not None:
            msg["params"] = params
        return raw("POST", "/mcp", msg, h)

    def test_handshake(self):
        s, j = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(s, 200)
        self.assertEqual(j["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", j["result"]["capabilities"])
        s, j = self.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(j["result"]["protocolVersion"], "2025-11-25", "不认识的版本回自己最新的")
        s, j = self.rpc("notifications/initialized", mid=None)
        self.assertEqual((s, j), (202, None))
        self.assertEqual(raw("GET", "/mcp")[0], 405)
        self.assertEqual(self.rpc("没有这个方法")[1]["error"]["code"], -32601)

    def test_external_tools(self):
        r = R.rooms.create("给外面读", "目标E", WD)
        R.set_last(r.id)
        names = [t["name"] for t in self.rpc("tools/list")[1]["result"]["tools"]]
        self.assertEqual(names, ["list_rooms", "read_room", "post_to_room"])
        res = self.rpc("tools/call", {"name": "list_rooms", "arguments": {}})[1]["result"]
        self.assertIn(r.id, res["content"][0]["text"])
        res = self.rpc("tools/call", {"name": "post_to_room", "arguments": {"speaker": "codex", "text": "外面的 codex 说话", "room": r.id}})[1]["result"]
        self.assertFalse(res["isError"])
        self.assertEqual((r.messages[-1]["speaker"], r.messages[-1].get("origin")), ("codex", "external"))
        res = self.rpc("tools/call", {"name": "read_room", "arguments": {"room": r.id}})[1]["result"]
        self.assertIn("目标E", res["content"][0]["text"])
        self.assertIn("外面的 codex 说话", res["content"][0]["text"])
        res = self.rpc("tools/call", {"name": "post_to_room", "arguments": {"speaker": "你", "text": "冒充"}})[1]["result"]
        self.assertTrue(res["isError"])
        res = self.rpc("tools/call", {"name": "claim_files", "arguments": {"paths": ["x"]}})[1]["result"]
        self.assertTrue(res["isError"], "外面来的没有会议里的工具")

    def test_room_tools(self):
        r = R.rooms.create("会议里用", "目标R", WD)
        key = r.keys["codex"]
        names = [t["name"] for t in self.rpc("tools/list", key=key)[1]["result"]["tools"]]
        self.assertEqual(names, ["room_info", "post", "board", "claim_files", "release_files", "list_claims"])
        txt = self.rpc("tools/call", {"name": "room_info", "arguments": {}}, key=key)[1]["result"]["content"][0]["text"]
        self.assertIn("目标R", txt)
        # 讨论模式不让占
        res = self.rpc("tools/call", {"name": "claim_files", "arguments": {"paths": ["a.txt"]}}, key=key)[1]["result"]
        self.assertTrue(res["isError"])
        r.meta["mode"] = "work"
        res = self.rpc("tools/call", {"name": "claim_files", "arguments": {"paths": ["a.txt"], "minutes": 20, "reason": "要连着改"}}, key=key)[1]["result"]
        self.assertFalse(res["isError"])
        self.assertIn("占住了", res["content"][0]["text"])
        ok, hit = guard.touch((r.id, "claude"), F("a.txt"))
        self.assertFalse(ok)
        self.assertEqual(hit["reason"], "要连着改")
        txt = self.rpc("tools/call", {"name": "list_claims", "arguments": {}}, key=key)[1]["result"]["content"][0]["text"]
        self.assertIn("要连着改", txt)
        res = self.rpc("tools/call", {"name": "release_files", "arguments": {}}, key=key)[1]["result"]
        self.assertIn("放开了 1", res["content"][0]["text"])
        # 看板
        res = self.rpc("tools/call", {"name": "board", "arguments": {"op": "add", "kind": "todo", "text": "画三视图"}}, key=key)[1]["result"]
        self.assertFalse(res["isError"])
        self.assertEqual(r.meta["board"][-1]["text"], "画三视图")
        self.assertEqual(r.meta["board"][-1]["by"], "codex")
        # 中途发进度 + 文件：照登，不叫人
        before = len(r.messages)
        res = self.rpc("tools/call", {"name": "post", "arguments": {"text": "跑到一半了 @claude 别管", "files": [F("assets/c.png")]}}, key=key)[1]["result"]
        self.assertFalse(res["isError"])
        m = r.messages[-1]
        self.assertEqual((m["speaker"], len(m["attachments"])), ("codex", 1))
        self.assertFalse(r.busy_for("claude"), "中途的话不叫人")
        self.assertGreater(len(r.messages), before)

    def test_bad_key(self):
        s, j = self.rpc("tools/list", key="wrong-key")
        self.assertEqual(s, 401)

    def test_origin_check(self):
        s, _ = raw("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"Origin": "http://evil.example"})
        self.assertEqual(s, 403)
        s, _ = raw("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"Origin": "http://127.0.0.1:%d" % PORT})
        self.assertEqual(s, 200)


if __name__ == "__main__":
    unittest.main()
