# -*- coding: utf-8 -*-
"""几场会议同时跑：各跑各的、互不挡；会议列表的状态；外部发言按会议号；HTTP 接口带会议号。"""
import json, os, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from urllib.parse import quote

os.environ["ROUNDTABLE_LOG_DIR"] = tempfile.mkdtemp(prefix="rt_multi_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bridge import room as R, web, common

WD = tempfile.mkdtemp(prefix="rt_multi_wd_")
gate = {}      # 会议号 -> Event：放行它的 AI
started = {}   # (会议号, ai) -> 开跑时间


def fake(ai, prompt, sid, imgs, mode=None, room=None):
    started[(room.id, ai)] = time.time()
    ev = gate.get(room.id)
    if ev is not None:
        ev.wait(10)
    return True, "@用户 %s 回 %s" % (ai, room.title()), sid or ("sid-%s-%s" % (ai, room.id[-4:])), []


R.ai_caller = fake
srv = web.ExclusiveServer(("127.0.0.1", 0), web.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def call(method, path, obj=None, auth=True):
    h = {"Content-Type": "application/json"}
    if auth:
        h["X-RT-Token"] = web.RT_TOKEN
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), method=method,
                                 data=json.dumps(obj).encode() if obj is not None else None, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def rp(r, act):
    return "/api/r/%s/%s" % (quote(r.id), act)


def idle(*rooms, t=10):
    end = time.time() + t
    while time.time() < end:
        if not any(r.busy_for() for r in rooms):
            return True
        time.sleep(0.05)
    return False


class MultiRoom(unittest.TestCase):
    def setUp(self):
        gate.clear()
        started.clear()

    def test_rooms_run_in_parallel(self):
        a = R.rooms.create("甲", "", WD)
        b = R.rooms.create("乙", "", WD)
        gate[a.id] = threading.Event()
        a.handle_message("你", "@claude 慢慢想")
        time.sleep(0.2)
        self.assertTrue(a.busy_for("claude"))
        # 甲还在跑，乙照样能叫、能跑完
        b.handle_message("你", "@claude 你好")
        self.assertTrue(idle(b, t=5), "乙不该被甲挡住")
        self.assertIn("claude 回 乙", b.messages[-1]["text"])
        self.assertTrue(a.busy_for("claude"))
        gate[a.id].set()
        self.assertTrue(idle(a))
        self.assertIn("claude 回 甲", a.messages[-1]["text"])
        # 两场会议的会话各是各的
        self.assertNotEqual(a.meta["claude_session"], b.meta["claude_session"])

    def test_new_room_while_other_busy(self):
        a = R.rooms.create("忙的", "", WD)
        gate[a.id] = threading.Event()
        a.handle_message("你", "@codex 干长活")
        time.sleep(0.2)
        s, j = call("POST", "/api/rooms/new", {"title": "新的", "workdir": WD})
        self.assertEqual(s, 200)
        self.assertTrue(j["id"])
        self.assertTrue(a.busy_for("codex"), "开新会议不影响正在跑的")
        # 忙的那场不能删，别的能删
        self.assertEqual(call("POST", "/api/rooms/delete", {"id": a.id})[0], 409)
        self.assertEqual(call("POST", "/api/rooms/delete", {"id": j["id"]})[0], 200)
        gate[a.id].set()
        idle(a)

    def test_listing_status(self):
        a = R.rooms.create("看状态", "", WD)
        gate[a.id] = threading.Event()
        a.handle_message("你", "@claude 想想")
        time.sleep(0.2)
        s, j = call("GET", "/api/rooms")
        self.assertEqual(s, 200)
        row = next(x for x in j["rooms"] if x["id"] == a.id)
        self.assertEqual(row["running"], ["claude"])
        gate[a.id].set()
        idle(a)
        row = next(x for x in call("GET", "/api/rooms")[1]["rooms"] if x["id"] == a.id)
        self.assertEqual(row["running"], [])
        self.assertEqual(row["count"], len(a.messages))
        self.assertEqual(row["last_to_user"], len(a.messages) - 1, "AI @用户 的那条")

    def test_room_api_routes(self):
        a = R.rooms.create("接口", "旧目标", WD)
        self.assertEqual(call("POST", rp(a, "send"), {"text": "@claude 嗨"})[0], 200)
        idle(a)
        s, j = call("GET", rp(a, "messages") + "?since=0")
        self.assertEqual(s, 200)
        self.assertEqual(j["meeting"], a.id)
        self.assertEqual([m["speaker"] for m in j["messages"]], ["你", "claude"])
        self.assertEqual(call("POST", rp(a, "announcement"), {"text": "新目标"})[0], 200)
        st = call("GET", rp(a, "state"))[1]
        self.assertEqual(st["announcement"], "新目标")
        # 用户的接口只认浏览器
        self.assertEqual(call("POST", rp(a, "send"), {"text": "x"}, auth=False)[0], 403)
        # 没有的会议
        s, j = call("GET", "/api/r/%s/state" % quote("不存在的会议"))
        self.assertEqual(s, 404)
        self.assertTrue(j.get("gone"))
        # 打开会议 = 记成"最近打开的"
        self.assertEqual(call("POST", rp(a, "open"), {})[0], 200)
        self.assertEqual(R.last_room(), a.id)

    def test_goal_in_prompt(self):
        a = R.rooms.create("目标", "把第三章写完", WD)
        seen = []
        R.ai_caller = lambda ai, prompt, sid, imgs, mode=None, room=None: (seen.append(prompt), (True, "@用户 好", sid or "s1", []))[1]
        try:
            a.handle_message("你", "@claude 开始")
            idle(a)
            self.assertIn("这场会议的目标：\n把第三章写完", seen[-1])
            a.write_announcement("改成写第四章")
            a.handle_message("你", "@claude 继续")
            idle(a)
            self.assertIn("会议目标改了，新的目标：\n改成写第四章", seen[-1])
            self.assertNotIn("这场会议的目标：", seen[-1])
            a.handle_message("你", "@claude 再来")
            idle(a)
            self.assertNotIn("目标", seen[-1].split("新消息：")[0], "目标没改就不重复递")
        finally:
            R.ai_caller = fake

    def test_external_post_room(self):
        a = R.rooms.create("外部甲", "", WD)
        b = R.rooms.create("外部乙", "", WD)
        R.set_last(a.id)
        s, j = call("POST", "/api/post", {"speaker": "claude", "text": "不带会议号"}, auth=False)
        self.assertEqual((s, j["room"]), (200, a.id))
        s, j = call("POST", "/api/post", {"speaker": "codex", "text": "带会议号", "room": b.id}, auth=False)
        self.assertEqual((s, j["room"]), (200, b.id))
        self.assertEqual(b.messages[-1]["text"], "带会议号")
        self.assertEqual(b.messages[-1].get("origin"), "external")
        self.assertEqual(call("POST", "/api/post", {"speaker": "codex", "text": "x", "room": "没有"}, auth=False)[0], 404)
        self.assertEqual(call("POST", "/api/post", {"speaker": "你", "text": "冒充用户"}, auth=False)[0], 400)

    def test_stop_only_this_room(self):
        a = R.rooms.create("停甲", "", WD)
        b = R.rooms.create("停乙", "", WD)
        gate[a.id] = threading.Event()
        gate[b.id] = threading.Event()
        a.handle_message("你", "@claude 长活")
        b.handle_message("你", "@claude 长活")
        time.sleep(0.2)
        a.stop_ais(["claude"])
        self.assertTrue(b.busy_for("claude"), "停甲不该动乙")
        self.assertEqual(b.state["epoch"]["claude"], 0)
        gate[a.id].set()
        gate[b.id].set()
        idle(a, b)

    def test_reload_from_disk(self):
        a = R.rooms.create("重读", "目标X", WD)
        a.handle_message("你", "@claude 记住")
        idle(a)
        rid, n = a.id, len(a.messages)
        R.rooms.unload(rid)
        b = R.rooms.get(rid)
        self.assertIsNot(a, b)
        self.assertEqual(len(b.messages), n)
        self.assertEqual(b.goal(), "目标X")
        self.assertEqual(b.meta["claude_session"], a.meta["claude_session"])

    def test_legacy_page_endpoints(self):
        a = R.rooms.create("老页面", "", WD)
        R.set_last(a.id)
        s, j = call("GET", "/api/state")
        self.assertEqual((s, j["meeting"], j.get("reload")), (200, a.id, True))
        self.assertEqual(call("POST", "/api/send", {"text": "x"})[0], 409)


if __name__ == "__main__":
    unittest.main()
