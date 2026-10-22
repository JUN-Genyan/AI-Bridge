# -*- coding: utf-8 -*-
"""HTTP 接口和启动。

会议相关的接口都带会议号：/api/r/<会议号>/<动作>，每个浏览器标签页可以开着不同的会议，互不影响。
代表用户的操作只认浏览器（Sec-Fetch-Site: same-origin，或页面注入的 X-RT-Token）；
外部程序用 /api/post 或 /mcp；钩子用 /api/guard/*（带钥匙）。"""
import hmac, json, mimetypes, os, re, secrets, socket, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

from . import agents, mcp, room as R
from .common import (AIS, EXTERNAL_SPEAKERS, HERE, HOST, MAX_ANN, MAX_BODY, MAX_UPLOAD, MEET_DIR, PORT, PROJECT,
                     in_dir, kind_of)
from .guard import USER, guard

RT_TOKEN = secrets.token_urlsafe(24)   # 每次启动随机；页面加载时注入 HTML，前端每个请求带 X-RT-Token
USER_ONLY_MSG = "这个接口只给网页上的用户用；外部程序请用 /api/post 或 /mcp 并写明自己的身份"
STATIC = os.path.join(HERE, "static")
INDEX = os.environ.get("ROUNDTABLE_INDEX") or os.path.join(HERE, "index.html")   # 页面；开发时可以指到别的文件
ROOM_PATH = re.compile(r"^/api/r/([^/]+)/([a-z/]+)$")


class Handler(BaseHTTPRequestHandler):
    server_version = "AIBridge"

    def log_message(self, *a):
        pass

    # ---------- 回应 ----------
    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", extra)

    def _length(self):
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return -1

    def _read_json(self):
        n = self._length()
        if n < 0 or n > MAX_BODY:
            self._json(413, {"error": "too large"})
            return None
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            self._json(400, {"error": "bad json"})
            return None
        if not isinstance(data, dict):
            self._json(400, {"error": "bad json"})
            return None
        return data

    def _from_browser(self):
        """代表用户的操作只认浏览器：Sec-Fetch-Site: same-origin，或页面注入的 X-RT-Token。
        这是防误用，不是防故意伪造（curl 想伪造请求头是做得到的）。"""
        if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "same-origin":
            return True
        tok = self.headers.get("X-RT-Token") or ""
        return bool(tok) and hmac.compare_digest(tok.encode("utf-8"), RT_TOKEN.encode("utf-8"))

    def _bearer(self):
        a = self.headers.get("Authorization") or ""
        if a.lower().startswith("bearer "):
            return a[7:].strip()
        return (self.headers.get("X-Bridge-Key") or "").strip()

    # ---------- 静态 ----------
    def _static(self, name, ctype):
        p = os.path.join(STATIC, name)
        if not os.path.isfile(p):
            return self._send(404, b"not found", "text/plain")
        with open(p, "rb") as f:
            body = f.read()
        extra = {"Service-Worker-Allowed": "/"} if name == "sw.js" else None
        self._send(200, body, ctype, extra)

    def _serve_file(self, rel):
        rel = unquote(rel)
        if "\x00" in rel:
            return self._send(400, b"bad", "text/plain")
        base = os.path.realpath(MEET_DIR)
        full = os.path.realpath(os.path.join(base, rel.replace("/", os.sep)))
        if not in_dir(full, base):
            return self._send(403, b"forbidden", "text/plain")
        parts = os.path.relpath(full, base).split(os.sep)
        if len(parts) < 3 or parts[1] != "files":  # 只放行 <会议>/files/ 里的东西
            return self._send(403, b"forbidden", "text/plain")
        if not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        safe = kind_of(full) in ("image", "audio") or ctype == "application/pdf"
        if ctype == "image/svg+xml":
            safe = False
        total = os.path.getsize(full)
        start, end, code = 0, total - 1, 200
        mt = re.match(r"bytes=(\d*)-(\d*)$", (self.headers.get("Range", "")).strip())
        if mt and (mt.group(1) or mt.group(2)):
            if mt.group(1):
                start = int(mt.group(1))
                if mt.group(2):
                    end = min(end, int(mt.group(2)))
            else:
                start = max(0, total - int(mt.group(2)))
            if start > end or start >= total:
                return self._send(416, b"", "text/plain", {"Content-Range": "bytes */%d" % total})
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=3600")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
        if not safe:
            self.send_header("Content-Disposition", "attachment")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(full, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(left, 1 << 20))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # ---------- GET ----------
    def do_GET(self):
        path, _, qs = self.path.partition("?")
        q = parse_qs(qs)
        if path in ("/", "/index.html"):
            with open(INDEX, "rb") as f:
                html = f.read().replace(b"__RT_TOKEN__", RT_TOKEN.encode("ascii"))
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/sw.js":
            return self._static("sw.js", "text/javascript; charset=utf-8")
        if path == "/manifest.webmanifest":
            return self._static("manifest.webmanifest", "application/manifest+json")
        if path.startswith("/static/") and re.match(r"^/static/[\w.\-]+$", path):
            name = path[len("/static/"):]
            return self._static(name, mimetypes.guess_type(name)[0] or "application/octet-stream")
        if path.startswith("/files/"):
            return self._serve_file(path[len("/files/"):])
        if path == "/mcp":
            return self._json(405, {"error": "这个 MCP 服务只收 POST"}, {"Allow": "POST"})
        if path == "/api/rooms":
            return self._json(200, {"rooms": R.rooms.listing(), "last": R.last_room(), "default_workdir": PROJECT})
        if path == "/api/trash":
            return self._json(200, {"trash": R.list_trash()})
        if path == "/api/claims":
            return self._json(200, {"claims": guard.listing()})
        if path in ("/api/state", "/api/messages"):   # 老页面（服务器换了新版、页面还没刷新）：按最近打开的那场回
            rid = R.last_room()
            r = R.rooms.get(rid) if rid else None
            if r is None:
                return self._json(200, {"meeting": None, "messages": [], "next": 0, "running": [], "queued": 0, "reload": True})
            if path == "/api/state":
                return self._json(200, dict(r.api_state(), reload=True))
            return self._json(200, dict(r.messages_since(_int(q, "since")), reload=True))
        m = ROOM_PATH.match(path)
        if m:
            r = R.rooms.get(unquote(m.group(1)))
            if r is None:
                return self._json(404, {"error": "没有这场会议（可能删了）", "gone": True})
            act = m.group(2)
            if act == "messages":
                return self._json(200, r.messages_since(_int(q, "since")))
            if act == "state":
                return self._json(200, r.api_state())
            if act == "tools":
                ai = (q.get("ai") or [""])[0]
                if ai not in AIS:
                    return self._json(400, {"error": "ai 只能是 claude / codex"})
                return self._json(200, r.tools_info(ai))
        return self._send(404, b"not found", "text/plain")

    do_HEAD = do_GET

    # ---------- POST ----------
    def do_POST(self):
        path, _, qs = self.path.partition("?")
        if path == "/mcp":
            return self._mcp()
        if path.startswith("/api/guard/"):
            return self._guard(path[len("/api/guard/"):])
        if path == "/api/post":
            return self._external_post()
        if not self._from_browser():
            return self._json(403, {"error": USER_ONLY_MSG})
        m = ROOM_PATH.match(path)
        if m:
            r = R.rooms.get(unquote(m.group(1)))
            if r is None:
                return self._json(404, {"error": "没有这场会议（可能删了）", "gone": True})
            return self._room_post(r, m.group(2), qs)
        if path == "/api/upload" or path == "/api/send":
            return self._json(409, {"error": "页面是旧版的，刷新一下再发", "reload": True})
        data = self._read_json()
        if data is None:
            return
        if path == "/api/rooms/new":
            ann = str(data.get("announcement", ""))
            if len(ann.encode("utf-8")) > MAX_ANN:
                return self._json(413, {"error": "too large (max 16KB)"})
            wd = str(data.get("workdir") or "").strip().strip('"') or PROJECT
            wd = os.path.normpath(os.path.expandvars(os.path.expanduser(wd)))
            if not os.path.isabs(wd) or not os.path.isdir(wd):
                return self._json(400, {"error": "工作文件夹不存在：" + wd})
            r = R.rooms.create(str(data.get("title", "")), ann, wd)
            return self._json(200, {"ok": True, "id": r.id})
        if path == "/api/rooms/archive":
            ok, msg = R.set_archived(str(data.get("id", "")), bool(data.get("on")))
        elif path == "/api/rooms/delete":
            ok, msg = R.trash_meeting(str(data.get("id", "")))
        elif path == "/api/trash/restore":
            ok, msg, _new = R.restore_meeting(str(data.get("id", "")))
        elif path == "/api/trash/purge":
            ok, msg = R.purge_all()
        elif path == "/api/claims/release":
            it = guard.force_release(str(data.get("key", "")))
            if it is None:
                return self._json(409, {"ok": False, "error": "已经放开了"})
            r = R.rooms.loaded.get(it["room"])
            if r is not None:
                r.add_message("系统", "你放开了 %s 占着的 %s" % (it["ai"], it["shown"]))
            return self._json(200, {"ok": True})
        elif path == "/api/claims/mine":   # 用户自己占：我在手动改这些，AI 别动
            paths = [os.path.normpath(os.path.expanduser(str(p).strip().strip('"'))) for p in data.get("paths") or [] if str(p).strip()]
            if not paths or not all(os.path.isabs(p) for p in paths):
                return self._json(400, {"error": "填完整路径"})
            res = guard.claim(("", USER), paths, data.get("minutes"), str(data.get("reason") or "用户自己在改"), "")
            return self._json(200, {"ok": True, "claimed": [p for p, ok_, _ in res if ok_],
                                    "blocked": [{"path": p, "by": hit["ai"]} for p, ok_, hit in res if not ok_]})
        else:
            return self._send(404, b"not found", "text/plain")
        return self._json(200 if ok else 409, {"ok": ok, "error": msg})

    def _room_post(self, r, act, qs):
        if act == "upload":
            n = self._length()
            if n <= 0:
                return self._json(400, {"error": "empty"})
            if n > MAX_UPLOAD:
                return self._json(413, {"error": "too large (max 25MB)"})
            name = (parse_qs(qs).get("name") or [""])[0]
            try:
                full = r.save_upload(name, self.rfile.read, n)
            except Exception as e:
                return self._json(500, {"error": str(e)})
            return self._json(200, R.make_attachment(full))
        data = self._read_json()
        if data is None:
            return
        if act == "open":   # 页面打开了这场会议：记成"最近打开的"（外部程序不指定会议时发到这里）
            R.set_last(r.id)
            return self._json(200, {"ok": True})
        if act == "send":
            text = str(data.get("text", "")).strip()
            atts = r.clean_attachments(data.get("attachments"))
            if not text and not atts:
                return self._json(400, {"error": "empty"})
            reply = r.make_reply(data.get("reply_to"), data.get("reply_part")) if data.get("reply_to") is not None else None
            R.set_last(r.id)
            r.handle_message("你", text, atts, reply_to=reply)
            return self._json(200, {"ok": True})
        if act == "announcement":
            text = str(data.get("text", ""))
            if len(text.encode("utf-8")) > MAX_ANN:
                return self._json(413, {"error": "too large (max 16KB)"})
            r.write_announcement(text)
            return self._json(200, {"ok": True})
        if act == "board":
            try:
                ids = [int(data.get("id"))] if data.get("id") is not None else []
            except (TypeError, ValueError):
                return self._json(400, {"error": "bad id"})
            res = r.board_op("你", str(data.get("op", "")), str(data.get("kind", "")), str(data.get("text", "")), ids)
            if not res:
                return self._json(400, {"error": "没改成"})
            r.board_note("你", [res])
            return self._json(200, {"ok": True})
        if act == "mode":
            ok, msg = r.set_mode(str(data.get("mode", "")))
            return self._json(200 if ok else 409, {"ok": ok, "error": msg})
        if act in ("undo", "undo/preview"):
            try:
                seq = int(data.get("step"))
            except (TypeError, ValueError):
                return self._json(400, {"ok": False, "error": "bad step"})
            if act == "undo/preview":
                ok, msg, plan = r.undo_preview(seq)
                return self._json(200 if ok else 409, {"ok": ok, "error": msg, "plan": plan})
            ok, msg = r.undo_step(seq)
            return self._json(200 if ok else 409, {"ok": ok, "error": msg})
        if act == "models":
            ok, msg, changed = r.set_models(str(data.get("ai", "")), str(data.get("model") or ""), str(data.get("effort") or ""))
            return self._json(200 if ok else 409, {"ok": ok, "error": msg, "model_changed": changed})
        if act == "tools":
            ok, msg = r.set_tools(str(data.get("ai", "")), data.get("tools"))
            return self._json(200 if ok else 409, {"ok": ok, "error": msg})
        if act == "handoff":
            ok, msg = r.set_handoff(str(data.get("ai", "")), bool(data.get("on")))
            return self._json(200 if ok else 409, {"ok": ok, "error": msg})
        if act == "wrapup":
            ok, msg = r.request_wrapup()
            return self._json(200 if ok else 409, {"ok": ok, "error": msg})
        if act == "stop":
            who = str(data.get("ai") or "")
            if who and who not in AIS:
                return self._json(400, {"error": "ai 只能是 claude / codex，或者不填（全停）"})
            return self._json(200, {"ok": True, "msg": r.stop_ais([who] if who else AIS)})
        if act == "compact":
            who = str(data.get("ai") or "")
            if who and who not in AIS:
                return self._json(400, {"error": "ai 只能是 claude / codex，或者不填（两个都压）"})
            todo = r.request_compact([who] if who else AIS)
            if not todo:
                return self._json(409, {"error": "%s已经在压缩了" % (who or "两个都")})
            return self._json(200, {"ok": True, "ais": todo})
        return self._send(404, b"not found", "text/plain")

    def _external_post(self):
        """给外部程序用：桌面应用里的 Claude、终端里的 Claude / Codex 等。room 不填 = 用户最近打开的那场。"""
        data = self._read_json()
        if data is None:
            return
        speaker = str(data.get("speaker") or "").strip()
        if speaker not in EXTERNAL_SPEAKERS:
            return self._json(400, {"error": "speaker 必填，且只能是：" + "、".join(EXTERNAL_SPEAKERS)})
        rid = str(data.get("room") or "").strip() or R.last_room()
        r = R.rooms.get(rid) if rid else None
        if r is None:
            return self._json(409 if not rid else 404, {"error": "还没有会议，先开一个" if not rid else "没有这场会议：" + rid})
        text = str(data.get("text", "")).strip()
        if not text:
            return self._json(400, {"error": "empty"})
        r.handle_message(speaker, text, origin="external")
        return self._json(200, {"ok": True, "speaker": speaker, "origin": "external", "room": r.id})

    def _mcp(self):
        origin = self.headers.get("Origin")
        if origin and not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
            return self._json(403, {"error": "origin not allowed"})   # 防网页借浏览器打本机端口（DNS rebinding）
        n = self._length()
        if n < 0 or n > MAX_BODY * 4:
            return self._json(413, {"error": "too large"})
        code, resp = mcp.handle_http(self.rfile.read(n), self._bearer())
        if resp is None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(code, resp)

    def _guard(self, kind):
        """编辑前的钩子来问：能不能改这个文件。钥匙 = 会议室起 AI 时给的。"""
        who = R.who_by_key(self._bearer())
        n = self._length()
        if n < 0 or n > MAX_BODY * 16:
            return self._json(413, {"error": "too large"})
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            data = {}
        if who is None:   # 钥匙对不上（会议室重启过之类）：不挡，免得 AI 白白卡住
            return self._json(200, agents.guard_allow(kind))
        r = R.rooms.get(who[0])
        return self._json(200, agents.guard_decide(kind, r, who[1], data))


def _int(q, name):
    try:
        return max(0, int((q.get(name) or ["0"])[0]))
    except ValueError:
        return 0


class ExclusiveServer(ThreadingHTTPServer):
    """端口独占：已经有会议室在跑时绑定失败，而不是两个进程悄悄共用一个端口。"""
    allow_reuse_address = os.name != "nt"   # Windows 的 SO_REUSEADDR 会允许重复绑定；别的系统上它只管 TIME_WAIT，留着
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    url = "http://%s:%d/" % (HOST, PORT)
    try:
        srv = ExclusiveServer((HOST, PORT), Handler)
    except OSError:
        # 2026-10-14：Windows 上默认的 SO_REUSEADDR 让第二个会议室也绑上了同一个端口，请求落到老进程上，新代码不生效还看不出来
        print("端口 %d 已经被占了：多半已经有一个会议室在跑（可能是桌面快捷方式在后台开的）。直接打开 %s ，或者先关掉那个再开。" % (PORT, url))
        return
    agents.start_reaper()
    print("会议室已启动：" + url)
    if os.environ.get("ROUNDTABLE_NO_BROWSER") != "1":
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
