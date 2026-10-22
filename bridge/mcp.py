# -*- coding: utf-8 -*-
"""会议室自己的 MCP 服务（Streamable HTTP，只回 JSON，不开 SSE），地址 http://127.0.0.1:<端口>/mcp。

两种来客：
- 会议里的 AI：会议室起 claude / codex 时把这场会议给它的钥匙放进请求头（Authorization: Bearer <钥匙>）。
  它能看目标 / 看板 / 谁占着哪些文件、中途发进度和文件、改看板、占文件 / 放开文件。
- 外面的 AI（用户桌面版、终端里的 claude / codex，不带钥匙）：能列会议、读会议记录、以 claude / codex 的身份往会议里发言，
  跟 /api/post 一样（消息带 origin=external）。"""
import json, os, time

from . import room as R
from .common import AIS, EXTERNAL_SPEAKERS, MAX_ATTACH, mode_text
from .guard import block_text, guard

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "ai-bridge", "title": "AI Bridge 会议室", "version": "2.0"}
INSTRUCTIONS = "AI Bridge 会议室的工具：看会议目标、看板、谁占着哪些文件；中途往会议里发进度和文件；占文件防止两个 AI 同时改。"


def _obj(props, req=()):
    return {"type": "object", "properties": props, "required": list(req), "additionalProperties": False}


ROOM_TOOLS = [
    {"name": "room_info", "title": "这场会议的情况",
     "description": "看这场会议的目标、模式、看板、谁在跑，以及同一个文件夹里谁占着哪些文件。",
     "inputSchema": _obj({}), "annotations": {"readOnlyHint": True}},
    {"name": "post", "title": "中途发一句 / 发文件",
     "description": "在一轮还没说完的时候往会议里发一句进度，或者把本机文件（图片等）发给用户看。不会叫醒别人（里面的 @ 不叫人）。"
                    "最终的回答照常直接回复，不用这个。",
     "inputSchema": _obj({"text": {"type": "string", "description": "要说的话（可空，只发文件时）"},
                          "files": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_ATTACH,
                                    "description": "本机文件路径（完整路径或相对工作文件夹）"}})},
    {"name": "board", "title": "改看板",
     "description": "看板 = 这场会议的公告和待办。op=add 加一条（kind=todo 待办 / note 公告，text 内容）；op=done / undone 勾掉 / 取消勾选（ids）；op=delete 删（ids）。",
     "inputSchema": _obj({"op": {"type": "string", "enum": ["add", "done", "undone", "delete"]},
                          "kind": {"type": "string", "enum": ["todo", "note"]},
                          "text": {"type": "string"},
                          "ids": {"type": "array", "items": {"type": "integer"}}}, ["op"])},
    {"name": "claim_files", "title": "占文件",
     "description": "先占住接下来要连着改的一批文件或文件夹（文件夹会占住里面所有文件），别的 AI（这场会议的另一个、别的会议里的）就改不了。"
                    "用编辑工具改文件时会自动占一轮，只有要跨几轮连着改时才需要明着占。写清原因和大概要多久（分钟，默认 30，最多 240）。",
     "inputSchema": _obj({"paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                          "minutes": {"type": "integer", "minimum": 1, "maximum": 240},
                          "reason": {"type": "string"}}, ["paths"])},
    {"name": "release_files", "title": "放开文件",
     "description": "放开自己占的文件。paths 不填 = 放开自己占的全部。",
     "inputSchema": _obj({"paths": {"type": "array", "items": {"type": "string"}}})},
    {"name": "list_claims", "title": "谁占着哪些文件",
     "description": "列出现在被占着的文件（默认只列这场会议工作文件夹里的；all=true 列全部会议的）。",
     "inputSchema": _obj({"all": {"type": "boolean"}}), "annotations": {"readOnlyHint": True}},
]

EXT_TOOLS = [
    {"name": "list_rooms", "title": "列出会议",
     "description": "列出 AI Bridge 里的会议：会议号、标题、工作文件夹、谁在跑、消息数。",
     "inputSchema": _obj({"include_archived": {"type": "boolean"}}), "annotations": {"readOnlyHint": True}},
    {"name": "read_room", "title": "读会议记录",
     "description": "读一场会议的目标、看板和最近的消息。room 不填 = 用户最近打开的那场。",
     "inputSchema": _obj({"room": {"type": "string"}, "since": {"type": "integer", "minimum": 0},
                          "limit": {"type": "integer", "minimum": 1, "maximum": 200}}), "annotations": {"readOnlyHint": True}},
    {"name": "post_to_room", "title": "往会议里发言",
     "description": "以 claude 或 codex 的身份往会议里发一句（页面上显示成会议外的同名 AI 说的）。里面 @claude / @codex 会叫会议里的 AI 回话，@用户 会提醒用户。"
                    "room 不填 = 用户最近打开的那场。",
     "inputSchema": _obj({"room": {"type": "string"}, "speaker": {"type": "string", "enum": list(EXTERNAL_SPEAKERS)},
                          "text": {"type": "string"}}, ["speaker", "text"])},
]


class ToolError(Exception):
    pass


def _text(s, err=False):
    return {"content": [{"type": "text", "text": s}], "isError": bool(err)}


def _room_for(rid):
    rid = rid or R.last_room()
    r = R.rooms.get(rid) if rid else None
    if r is None:
        raise ToolError("没有这场会议：%s" % (rid or "（还没打开过会议）"))
    return r


def _claims_text(items):
    if not items:
        return "（现在没人占着文件）"
    out = []
    for c in items:
        how = "这一轮自动占着" if c["kind"] == "auto" else "明着占着，还有约 %s 分钟%s" % (c["mins_left"], "，原因：" + c["reason"] if c["reason"] else "")
        out.append("- %s%s：%s（%s）" % (c["shown"], "（整个文件夹）" if c["dir"] else "", c["who"], how))
    return "\n".join(out)


def call_room_tool(rid, ai, name, args):
    r = R.rooms.get(rid)
    if r is None:
        raise ToolError("这场会议已经不在了")
    who = (r.id, ai)
    if name == "room_info":
        st = r.api_state()
        lines = ["会议：%s（%s）" % (r.title(), "干活模式" if r.mode() == "work" else "讨论模式"),
                 "工作文件夹：%s" % r.workdir(), "目标：\n" + r.goal_text(), "看板：\n" + r.board_text(),
                 "在跑：%s" % ("、".join(st["running"]) or "没有"),
                 "这个文件夹里被占着的文件：\n" + _claims_text(st["claims"])]
        return "\n\n".join(lines)
    if name == "post":
        atts, errs = [], []
        for p in (args.get("files") or [])[:MAX_ATTACH]:
            try:
                atts.append(r.send_file(str(p)))
            except Exception as e:
                errs.append(str(e))
        m = r.post_progress(ai, str(args.get("text") or ""), atts)
        if m is None and not errs:
            raise ToolError("没写内容也没给文件")
        return ("发出去了" + ("（%d 个文件）" % len(atts) if atts else "")) + ("；没发成：" + "；".join(errs) if errs else "")
    if name == "board":
        op = str(args.get("op") or "")
        ids = [int(x) for x in args.get("ids") or [] if isinstance(x, int) or str(x).isdigit()]
        if op == "add":
            res = r.board_op(ai, "add", str(args.get("kind") or "todo"), str(args.get("text") or ""))
        elif op in ("done", "undone", "delete"):
            res = r.board_op(ai, "del" if op == "delete" else op, ids=ids)
        else:
            raise ToolError("op 只能是 add / done / undone / delete")
        if not res:
            raise ToolError("没改成（检查编号、内容；看板最多 100 条）")
        r.board_note(ai, [res])   # 两个 AI 都能看到（它自己下一轮也看到，确认改上了）
        return res
    if name == "claim_files":
        paths = [r.send_path(str(p)) for p in args.get("paths") or [] if str(p).strip()]
        if not paths:
            raise ToolError("paths 不能为空")
        if r.mode() != "work":
            raise ToolError("讨论模式下不能改文件，用不着占")
        res = guard.claim(who, paths, args.get("minutes"), str(args.get("reason") or ""), r.title())
        ok = [p for p, good, _ in res if good]
        bad = [(p, hit) for p, good, hit in res if not good]
        out = []
        if ok:
            out.append("占住了：\n" + "\n".join("- " + p for p in ok))
            r.system_note("%s 占住了 %d 个路径：%s" % (ai, len(ok), "、".join(os.path.basename(p.rstrip("\\/")) or p for p in ok[:6])))
        for p, hit in bad:
            out.append(block_text(p, hit))
        return "\n".join(out)
    if name == "release_files":
        paths = [r.send_path(str(p)) for p in args.get("paths") or [] if str(p).strip()]
        n = guard.release(who, paths or None)
        return "放开了 %d 个" % n
    if name == "list_claims":
        items = guard.listing()
        if not args.get("all"):
            from .common import in_dir
            items = [c for c in items if c["room"] == r.id or in_dir(c["shown"], r.workdir())]
        return _claims_text(items)
    raise ToolError("没有这个工具：%s" % name)


def call_ext_tool(name, args):
    if name == "list_rooms":
        out = []
        for x in R.rooms.listing():
            if x["archived"] and not args.get("include_archived"):
                continue
            out.append("- %s：%s（%s%s，%d 条消息%s）" % (x["id"], x["title"], x["workdir"], "，已归档" if x["archived"] else "",
                                                     x["count"], "，在跑：" + "、".join(x["running"]) if x["running"] else ""))
        return "\n".join(out) or "（还没有会议）"
    if name == "read_room":
        r = _room_for(args.get("room"))
        try:
            limit = max(1, min(200, int(args.get("limit") or 30)))
            since = max(0, int(args.get("since") or 0))
        except (TypeError, ValueError):
            raise ToolError("since / limit 要是整数")
        with r.lock:
            msgs = r.messages[since:]
        msgs = [m for m in msgs if not m.get("quiet")][-limit:]
        body = "\n\n".join("#%d %s %s" % (m["id"], m["time"], R.fmt_message(m)) for m in msgs) or "（没有消息）"
        return "会议：%s（%s）\n目标：\n%s\n\n看板：\n%s\n\n消息（共 %d 条，下次从 since=%d 读起）：\n%s" % (
            r.title(), r.id, r.goal_text(), r.board_text(), len(r.messages), len(r.messages), body)
    if name == "post_to_room":
        sp = str(args.get("speaker") or "")
        if sp not in EXTERNAL_SPEAKERS:
            raise ToolError("speaker 只能是 " + " / ".join(EXTERNAL_SPEAKERS))
        text = str(args.get("text") or "").strip()
        if not text:
            raise ToolError("text 不能为空")
        r = _room_for(args.get("room"))
        r.handle_message(sp, text, origin="external")
        return "发到「%s」了" % r.title()
    raise ToolError("没有这个工具：%s" % name)


def _result(rid, res):
    return {"jsonrpc": "2.0", "id": rid, "result": res}


def _error(rid, code, msg):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def handle_one(msg, who):
    """处理一条 JSON-RPC。who = (会议号, ai) 或 None（外面来的）。返回回应（通知 / 回应类的返回 None）。"""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, -32600, "invalid request")
    if "method" not in msg:
        return None   # 客户端回给我们的（我们不发请求），忽略
    mid, meth, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None   # 通知：notifications/initialized 之类
    if meth == "initialize":
        want = str(params.get("protocolVersion") or "")
        return _result(mid, {"protocolVersion": want if want in VERSIONS else VERSIONS[0],
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
    if meth == "ping":
        return _result(mid, {})
    if meth == "tools/list":
        return _result(mid, {"tools": ROOM_TOOLS if who else EXT_TOOLS})
    if meth in ("resources/list", "resources/templates/list"):
        return _result(mid, {"resources": []} if meth == "resources/list" else {"resourceTemplates": []})
    if meth == "prompts/list":
        return _result(mid, {"prompts": []})
    if meth == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(mid, -32602, "arguments 要是对象")
        try:
            if who:
                text = call_room_tool(who[0], who[1], name, args)
            else:
                text = call_ext_tool(name, args)
            return _result(mid, _text(text))
        except ToolError as e:
            return _result(mid, _text(str(e), err=True))
        except Exception as e:
            return _result(mid, _text("会议室内部错误：%s" % e, err=True))
    return _error(mid, -32601, "不支持 %s" % meth)


def handle_http(body, key):
    """POST /mcp 的正文。返回 (状态码, 回应对象或 None)。"""
    try:
        data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except Exception:
        return 400, _error(None, -32700, "parse error")
    who = None
    if key:
        who = R.who_by_key(key)
        if who is None:
            return 401, _error(None, -32001, "钥匙不对（会议室重启过？下一轮会换新的）")
    if isinstance(data, list):   # 老版本允许批量
        out = [x for x in (handle_one(m, who) for m in data) if x is not None]
        return (200, out) if out else (202, None)
    resp = handle_one(data, who)
    return (200, resp) if resp is not None else (202, None)
