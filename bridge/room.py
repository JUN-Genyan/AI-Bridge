# -*- coding: utf-8 -*-
"""一场会议 = 一个 Room。几个 Room 可以同时跑：各自的消息、排队、锁、AI 会话、codex 后台进程都分开。

同一个 Room 里的规矩跟原来一样：
1. 用户发言叫起来的 AI 并行回答（每个 AI 一个线程）；AI 之间的对话进这个 Room 自己的串行队列。
2. 同一个 AI 永远串行（它在这场会议里只有一个会话）：每个 AI 一把锁，无论来源都排队等它自己。
3. 增量在拿到锁之后才取快照，所以排着队的那段时间里新来的消息也会带上。

本文件后半段是会议目录的增删（新建、列表、归档、回收站）和 Rooms（已经读进内存的会议）。"""
import json, os, queue, re, secrets, shutil, threading, time
from datetime import datetime
from urllib.parse import quote

from . import agents, workspace as ws
from .common import (AIS, ATT_BASE, LAST_FILE, LOG_DIR, MAX_ATTACH, MAX_IMAGES, MAX_UPLOAD, MEET_DIR, MODES, PROJECT,
                     ROOM_RE, SID_RE, TRASH_DIR, in_dir, is_archived, kill_tree, kind_of, mentions, mode_key, mode_text,
                     norm, now_str, preamble, read_json, ref_safe, safe_title, sanitize_name, write_json)
from .guard import guard

# 测试时可替换：ai_caller(ai, prompt, sid, images, mode, room) -> (ok, text, sid, edits)
ai_caller = agents.call_ai
ai_native_compact = agents.compact_native   # (ai, sid, room) -> bool

QUOTE_LEN = 200
MAX_BOARD = 100
MAX_BOARD_TEXT = 500
BOARD_CMD = re.compile(r"^\s*[【\[](公告|待办|完成|删)[】\]]\s*(.*?)\s*$")
BOARD_KIND = {"公告": "note", "待办": "todo"}
KIND_CN = {"note": "公告", "todo": "待办"}
SEND_CMD = re.compile(r"^\s*[【\[]发送[】\]]\s*(.*?)\s*$")
MD_IMAGE = re.compile(r"!\[([^\]\n]*)\]\(\s*(<[^>\n]+>|(?:[^()\s]|\([^()\n]*\))(?:[^()\n]|\([^()\n]*\))*?)\s*\)")   # 路径里允许一层成对括号，比如 a(1).png
CLOSING = "现在轮到你（%s）回复。只输出你要说的话，不要加发言人前缀。"

# 每场会议每个 AI 一把钥匙：会议室起 claude / codex 时带上，它们的钩子、MCP 调用拿这把钥匙证明"我是哪场会议的哪个 AI"
KEYS = {}            # 钥匙 -> (会议号, ai)
keys_lock = threading.Lock()


def who_by_key(key):
    with keys_lock:
        return KEYS.get(key or "")


def default_meta(title, ann, wd=None):
    return {"title": title or "会议", "announcement": ann or "", "ann_ver": 1, "created": now_str(),
            "workdir": wd or PROJECT,
            "status": "open", "claude_session": None, "codex_session": None,
            "last_seen": {"claude": -1, "codex": -1}, "announcement_seen": {"claude": 0, "codex": 0},
            "handed_off": {"claude": False, "codex": False}, "returned": {"claude": False, "codex": False},
            "mode": "discuss", "mode_seen": {"claude": "discuss", "codex": "discuss"},
            "step_seq": 0, "steps": {},
            "board": [], "board_seq": 0, "ctx": {"claude": None, "codex": None},
            "models": {"claude": {"model": "", "effort": ""}, "codex": {"model": "", "effort": ""}},
            "tools": {a: agents.default_tools(a) for a in AIS},
            "goal_synced": {"codex": None},
            "old_sessions": {"claude": [], "codex": []}}


def fix_meta(m):
    d = default_meta("", "")
    for k, v in d.items():
        m.setdefault(k, v)
    for k in ("last_seen", "announcement_seen", "handed_off", "returned", "mode_seen", "ctx", "old_sessions"):
        for a in AIS:
            m[k].setdefault(a, d[k][a])
    if not isinstance(m.get("models"), dict):
        m["models"] = d["models"]
    for a in AIS:
        if not isinstance(m["models"].get(a), dict):
            m["models"][a] = {"model": "", "effort": ""}
        m["models"][a].setdefault("model", "")
        m["models"][a].setdefault("effort", "")
    if not isinstance(m.get("tools"), dict):
        m["tools"] = d["tools"]
    for a in AIS:
        m["tools"][a] = agents.fix_tools(a, m["tools"].get(a))
    if not isinstance(m.get("goal_synced"), dict):
        m["goal_synced"] = {"codex": None}
    return m


# ===== 附件 =====
def make_attachment(full):
    full = os.path.normpath(full)
    return {"name": os.path.basename(full).split("_", 1)[-1],
            "path": os.path.relpath(full, ATT_BASE).replace("\\", "/"),
            "url": "/files/" + quote(os.path.relpath(os.path.realpath(full), os.path.realpath(MEET_DIR)).replace("\\", "/")),
            "kind": kind_of(full), "size": os.path.getsize(full)}


def abs_path(att):
    return os.path.normpath(os.path.join(ATT_BASE, att["path"]))


def fmt_message(m):
    text = m["text"]
    atts = m.get("attachments") or []
    if not text and any(a["kind"] == "audio" for a in atts):
        text = "（语音，未转文字）"
    rt = m.get("reply_to")
    if rt:
        text = ("（回复 [%s] 那条里的这段：“%s”）%s" if rt.get("part") else "（回复 [%s] 的“%s”）%s") % (rt["speaker"], rt["text"], text)
    lines = [("[%s] %s" % (m["speaker"], text)).rstrip()]
    for a in atts:
        label = {"image": "图片", "audio": "音频"}.get(a["kind"], "文件")
        lines.append("[附件 %s] %s" % (label, abs_path(a)))
    return "\n".join(lines)


def delta_images(msgs):
    imgs = []
    for m in reversed(msgs):
        for a in reversed(m.get("attachments") or []):
            if a["kind"] == "image":
                p = abs_path(a)
                if os.path.isfile(p) and p not in imgs:
                    imgs.append(p)
    return list(reversed(imgs[:MAX_IMAGES]))


def md_local_path(target):
    """markdown 图片的目标是不是本机路径；是就返回路径，网址 / data: 之类返回 None。"""
    from urllib.parse import unquote
    t = target.strip().strip("<>").strip()
    if t.lower().startswith("file:"):
        t = unquote(re.sub(r"^file:/*", "", t, flags=re.I))
        if not re.match(r"^[A-Za-z]:", t):
            t = "/" + t
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]+:(?![\\/])", t) and not re.match(r"^[A-Za-z]:[\\/]", t):
        return None   # http:、data: 这类
    if re.match(r"^(//|https?:)", t, re.I):
        return None
    return t


WRAPUP_TEXT = (
    "【落盘】用户点了落盘按钮。@claude 请按工作文件夹里的 wrapup 技能（.claude/skills/wrapup/SKILL.md）把这场会议到目前为止定下的东西落盘，"
    "技能里怎么写就怎么做。"
    "然后 git commit 并 push：只提交这次落盘改到的文件和改动块，工作区里别人没提交的改动不要带上。"
    "技能里要是有「提醒用户 /compact」之类要用户在终端里敲的步骤，在会议室里省掉。"
    "这一轮不管会议在什么模式，你都有完整权限。没有能落盘的结论就直说，不要硬写。"
    "做完 @用户 汇报：改了哪些文件、提交号、推送成没成。"
)

WRAPUP_GENERIC = (
    "【落盘】用户点了落盘按钮。@claude 请把这场会议到目前为止定下的东西写进工作文件夹（{wd}）里合适的文档："
    "先看这个文件夹有没有自己的规矩（CLAUDE.md / AGENTS.md / README 之类）并照着做；没有就写进最相关的现有文档，"
    "实在没有合适的，就新建一份 .md 会议纪要（已定结论 + 没解决的问题）。"
    "如果这是 git 仓库：git commit，有远端就 push；只提交这次落盘改到的文件和改动块，工作区里别人没提交的改动不要带上。"
    "这一轮不管会议在什么模式，你都有完整权限。没有能落盘的结论就直说，不要硬写。"
    "做完 @用户 汇报：改了哪些文件、提交号、推送成没成。"
)


class Room:
    def __init__(self, rid, d, meta, messages):
        self.id, self.dir, self.meta, self.messages = rid, d, meta, messages
        self.lock = threading.RLock()
        self.pending = []      # 已排上（队列里或在等自己的锁）还没开始的 ai，避免同一个人被重复叫
        self.state = {"running": [], "chain": 0,         # running：正在跑的名字列表（压缩时是 "compact:claude" 这样）
                      "compacting": set(),                # 排上或正在压缩的 AI（每个 AI 各压各的）
                      "force_work": set(),                # 下一轮不管会议在什么模式都按干活模式叫的 AI（落盘用）
                      "epoch": {a: 0 for a in AIS},       # 停了谁就给谁加一；排队时记下，开跑时对不上就不跑
                      "stopped": set()}                   # 被"停"打断、还没收尾的 AI
        self.live = {}        # ai -> {"text": 这一轮最近说的一句中途的话, "time": 时:分:秒, "since": 开跑的时间戳}
        self.last_ctx = {}    # call_ai 填，process_job 成功后搬进 meta["ctx"]
        self.procs = {}       # ai -> 正在跑的子进程（claude -p），"停"的时候连同子孙进程一起杀
        self.ai_locks = {a: threading.Lock() for a in AIS}
        self.jobs = queue.Queue()   # 这场会议 AI 之间的对话：(标签, 类型, ai)，一个跑完下一个才开始
        self.codex = None     # agents.CodexServer，第一次叫 codex 时起
        self.keys = {}
        with keys_lock:
            for a in AIS:
                k = secrets.token_urlsafe(18)
                self.keys[a] = k
                KEYS[k] = (rid, a)
        self.closed = False
        self.touched = time.time()
        threading.Thread(target=self._worker, daemon=True, name="room-" + rid).start()
        threading.Thread(target=agents.prewarm, args=(self.workdir(),), daemon=True).start()

    # ---------- 基本 ----------
    def workdir(self):
        """这场会议的工作文件夹：两个 AI 在这里跑、git 存档 / 撤回也在这里。开会时定死（claude 的会话按文件夹存，中途换了接不上）。"""
        return self.meta.get("workdir") or PROJECT

    def title(self):
        return self.meta.get("title") or self.id

    def session_name(self):
        """AI 那边的会话名 = 会议名（用户 2026-10-18 定），在 claude / codex 的会话列表里能认出是哪场会议。"""
        return (self.meta.get("title") or "").strip()[:80]

    def mode(self):
        return self.meta.get("mode", "discuss")

    def ref_name(self):
        return self.meta.get("ref_name") or ref_safe(self.id)

    def goal(self):
        return (self.meta.get("announcement") or "").strip()

    def goal_text(self):
        return self.goal() or "（还没写目标）"

    def save_meta(self):
        with self.lock:
            if not self.closed:
                write_json(os.path.join(self.dir, "meeting.json"), self.meta)

    def close(self):
        """会议移进回收站前：停后台、钥匙作废。"""
        with self.lock:
            self.closed = True
        self.jobs.put(None)
        agents.stop_codex(self)
        with keys_lock:
            for k in self.keys.values():
                KEYS.pop(k, None)
        guard.release_room(self.id)

    def add_message(self, speaker, text, attachments=None, to_user=False, quiet=False, step=None, not_for=None, origin=None, reply_to=None):
        now = datetime.now()
        with self.lock:
            m = {"id": len(self.messages), "speaker": speaker, "text": text, "time": now.strftime("%H:%M:%S")}
            if origin:
                m["origin"] = origin   # 不显示；external = 会议室外的同名 AI 经 /api/post 发的
            if attachments:
                m["attachments"] = attachments
            if to_user:
                m["to_user"] = True
            if quiet:
                m["quiet"] = True
            if step is not None:
                m["step"] = step
            if not_for:
                m["not_for"] = not_for
            if reply_to:
                m["reply_to"] = reply_to
            self.messages.append(m)
            self.touched = time.time()
            if not self.closed:
                with open(os.path.join(self.dir, "log.md"), "a", encoding="utf-8") as f:
                    f.write("### %s · %s%s\n%s\n" % (speaker, m["time"], "（→ 用户）" if to_user else "", text))
                    for at in attachments or []:
                        f.write("附件：%s\n" % at["path"])
                    if reply_to:
                        f.write("（回复 %s 的：%s）\n" % (reply_to["speaker"], reply_to["text"]))
                    f.write("\n")
                with open(os.path.join(self.dir, "log.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
        return m

    def system_note(self, text):
        self.add_message("系统", text, quiet=True)

    # ---------- 附件 ----------
    def files_dir(self):
        return os.path.join(self.dir, "files")

    def save_upload(self, name, reader, size):
        """从 reader 读 size 字节，写进这场会议的 files/ 下，返回完整路径。"""
        fd_ = self.files_dir()
        os.makedirs(fd_, exist_ok=True)
        root, ext = os.path.splitext("%s_%s" % (datetime.now().strftime("%H%M%S"), sanitize_name(name)))
        full, i = os.path.join(fd_, root + ext), 1
        while True:
            if not in_dir(full, fd_):
                raise ValueError("bad path")
            try:
                f = open(full, "xb")
                break
            except FileExistsError:
                i += 1
                full = os.path.join(fd_, "%s_%d%s" % (root, i, ext))
        try:
            with f:
                left = size
                while left > 0:
                    chunk = reader(min(left, 1 << 20))
                    if not chunk:
                        raise IOError("short body")
                    f.write(chunk)
                    left -= len(chunk)
        except Exception:
            try:
                os.remove(full)
            except Exception:
                pass
            raise
        return full

    def clean_attachments(self, raw):
        """校验客户端传来的附件：只认这场会议 files 目录里真实存在的文件，其余信息由服务端重算。"""
        out = []
        fd_ = self.files_dir()
        if not isinstance(raw, list):
            return out
        for a in raw[:MAX_ATTACH]:
            if not isinstance(a, dict):
                continue
            full = os.path.normpath(os.path.join(ATT_BASE, str(a.get("path", ""))))
            if in_dir(full, fd_) and os.path.isfile(full):
                out.append(make_attachment(full))
        return out

    def send_path(self, path):
        """AI 写的路径 → 本机完整路径（去引号、展开 ~ 和环境变量、相对路径按工作目录）。"""
        p = path.strip().strip("`'\"“”‘’<>").strip()
        if not p:
            return ""
        p = os.path.expandvars(os.path.expanduser(p))
        if not os.path.isabs(p):
            p = os.path.join(self.workdir(), p)
        return os.path.normpath(p)

    def send_file(self, path):
        """把 AI 指名的本机文件复制进这场会议 files/，返回附件；不行就抛 ValueError（原因给人看）。"""
        p = self.send_path(path)
        if not p:
            raise ValueError("没写路径")
        if not os.path.isfile(p):
            raise ValueError("找不到文件 %s" % p)
        size = os.path.getsize(p)
        if size > MAX_UPLOAD:
            raise ValueError("%s 太大（%.1f MB，上限 %d MB）" % (p, size / 1048576, MAX_UPLOAD // 1048576))
        fd_ = self.files_dir()
        if in_dir(p, fd_):   # 本来就是会议附件，不用再复制一份
            return make_attachment(p)
        with open(p, "rb") as f:
            full = self.save_upload(os.path.basename(p), f.read, size)
        return make_attachment(full)

    def apply_send_cmds(self, text):
        """把 AI 回复里的【发送】路径行收走、复制文件；正文里指向本机路径的 markdown 图片 ![](路径) 也当发送（它们习惯这么写，网页又显示不了本地路径）。
        返回 (剩下的正文, [附件], [没发成的原因])。代码块里的不算。"""
        keep, atts, errs, fence, seen = [], [], [], False, set()

        def send(path, label):
            key = os.path.normcase(self.send_path(path))
            if key and key in seen:
                return True   # 同一个文件写了两遍（比如 codex 自己写了【发送】，会议室又按生图结果补了一行），只发一次
            if len(atts) >= MAX_ATTACH:
                errs.append("一次最多发 %d 个，%s 没发" % (MAX_ATTACH, label))
                return False
            try:
                atts.append(self.send_file(path))
                seen.add(key)
                return True
            except Exception as e:
                errs.append(str(e) if isinstance(e, ValueError) else "%s 复制失败：%s" % (label, e))
                return False

        def md_img(m):
            if m.string[:m.start()].count("`") % 2:   # 在行内代码里
                return m.group(0)
            path = md_local_path(m.group(2))
            if path is None:
                return m.group(0)
            return "" if send(path, path) else m.group(0)   # 没发成就留着原文，至少看得到路径

        for line in text.split("\n"):
            if line.lstrip().startswith("```"):
                fence = not fence
            if fence:
                keep.append(line)
                continue
            m = SEND_CMD.match(line)
            if m:
                send(m.group(1), m.group(1))
                continue
            if "![" in line:
                new = MD_IMAGE.sub(md_img, line)
                if new != line and not new.strip():
                    continue   # 整行只有图片，收走后不留空行
                line = new.rstrip() if new != line else line
            keep.append(line)
        return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip(), atts, errs

    def send_note(self, by, errs):
        if errs:
            self.add_message("系统", "%s 的文件没发成：%s" % (by, "；".join(errs)))   # 不 quiet：发的人下一轮能看到

    # ---------- 目标（原来的"公告"）----------
    def write_announcement(self, text):
        with self.lock:
            self.meta["announcement"] = text
            self.meta["ann_ver"] += 1
            self.save_meta()
        self.add_message("系统", "会议目标改了", quiet=True)

    # ---------- 看板（公告 + 待办），用户和两个 AI 都能写 ----------
    def board_text(self):
        items = self.meta.get("board") or []
        if not items:
            return "（看板是空的）"
        out = []
        for it in items:
            tag = "已完成" if it.get("done") else KIND_CN.get(it["kind"], "公告")
            out.append("#%d [%s] %s（%s）" % (it["id"], tag, it["text"], it["by"]))
        return "\n".join(out)

    def board_op(self, by, op, kind=None, text="", ids=()):
        """改看板，返回给人看的一句话（没改成返回 None）。调用方负责发消息。"""
        with self.lock:
            board = self.meta["board"]
            if op == "add":
                text = (text or "").strip()[:MAX_BOARD_TEXT]
                if not text or kind not in KIND_CN or len(board) >= MAX_BOARD:
                    return None
                self.meta["board_seq"] += 1
                board.append({"id": self.meta["board_seq"], "kind": kind, "text": text, "by": by,
                              "done": False, "time": now_str()})
                self.save_meta()
                return "加了%s #%d：%s" % (KIND_CN[kind], self.meta["board_seq"], text)
            hit = [it for it in board if it["id"] in ids]
            if not hit:
                return None
            if op in ("done", "undone"):
                for it in hit:
                    it["done"] = op == "done"
                word = "勾掉了" if op == "done" else "取消勾选了"
            elif op == "del":
                self.meta["board"] = [it for it in board if it["id"] not in ids]
                word = "删了"
            else:
                return None
            self.save_meta()
            return word + "、".join("#%d" % it["id"] for it in hit)

    def apply_board_cmds(self, speaker, text):
        """把 AI 回复里的【公告】【待办】【完成】【删】行收走、改看板。返回 (剩下的正文, [改动描述])。代码块里的不算。"""
        keep, changes, fence = [], [], False
        for line in text.split("\n"):
            if line.lstrip().startswith("```"):
                fence = not fence
            m = None if fence else BOARD_CMD.match(line)
            if not m:
                keep.append(line)
                continue
            word, body = m.group(1), m.group(2)
            if word in BOARD_KIND:
                r = self.board_op(speaker, "add", BOARD_KIND[word], body)
            else:
                ids = [int(x) for x in re.findall(r"\d+", body)]
                r = self.board_op(speaker, "done" if word == "完成" else "del", ids=ids)
            changes.append(r or "（没改成：%s）" % line.strip())
        return "\n".join(keep).strip(), changes

    def board_note(self, by, changes, not_for=None):
        if changes:
            self.add_message("系统", "看板：%s %s" % (by, "；".join(changes)), not_for=not_for)

    # ---------- 上下文用量 ----------
    def note_ctx(self, ai, res):
        used, win = res.get("ctx_used"), res.get("ctx_win")
        if used:
            self.last_ctx[ai] = {"used": int(used), "window": int(win) if win else None, "time": now_str()}

    # ---------- 给 AI 的提示 ----------
    def delta_for(self, ai):
        """自 last_seen 之后、别人说的新消息（不含它自己、不含 quiet 的系统记号）。返回 (消息列表, 截止 id)。"""
        with self.lock:
            ls = self.meta["last_seen"][ai]
            upto = len(self.messages) - 1
            d = [m for m in self.messages[ls + 1:]
                 if (m["speaker"] != ai or m.get("origin") == "external") and not m.get("quiet") and m.get("not_for") != ai]
        return d, upto

    def build_prompt(self, ai, mode=None):
        """返回 (prompt, upto, ann_ver, images)。会话还没建 = 第一次叫，带前言和目标；否则只递增量。"""
        with self.lock:
            first = not self.meta.get(ai + "_session")
            d, upto = self.delta_for(ai)
            ver = self.meta["ann_ver"]
            parts = []
            mode = mode or self.mode()
            if first:
                parts += [preamble(mode, self.workdir()), "这场会议的目标：\n" + self.goal_text(), "看板：\n" + self.board_text()]
            else:
                seen = self.meta["mode_seen"].get(ai) or ""
                if seen != mode_key(mode):
                    head = "模式变了。" if seen.split(":")[0] != mode else "会议室规矩改了，以这一版为准（之前说的作废）："
                    parts.append(head + mode_text(mode))
                if self.meta["announcement_seen"][ai] != ver:
                    parts.append("会议目标改了，新的目标：\n" + self.goal_text())
                if self.meta["returned"][ai]:
                    parts.append("（你刚才在终端里跟用户单独干了一段活，那段你自己记得；下面是这期间会议室里的新消息）")
            body = "\n\n".join(fmt_message(m) for m in d) or "（没有新消息）"
            parts += [("会议记录：\n" if first else "新消息：\n") + body, CLOSING % ai]
        return "\n\n".join(parts), upto, ver, delta_images(d)

    # ---------- 模型 / effort ----------
    def own_models(self, ai):
        mo = (self.meta.get("models") or {}).get(ai) or {}
        return (mo.get("model") or "").strip(), (mo.get("effort") or "").strip()

    def effective_models(self, ai):
        """实际生效的 (模型, effort)：会议自己设了用自己的，没设用会议室默认 ROOM_DEFAULTS。"""
        m, e = self.own_models(ai)
        dm, de = agents.ROOM_DEFAULTS[ai]
        return (m or dm), (e or de)

    def model_label(self, ai):
        m, e = self.effective_models(ai)
        return "%s · %s" % (m or "默认", e or "默认")

    def model_info(self, ai):
        m, e = self.own_models(ai)
        return {"model": m, "effort": e, "label": self.model_label(ai), "default": " · ".join(agents.ROOM_DEFAULTS[ai]),
                "models": agents.model_options(ai), "efforts": agents.effort_options(ai, self.effective_models(ai)[0])}

    def set_models(self, ai, model, effort):
        """改某个 AI 在这场会议的模型 / effort（空 = 用会议室默认）。返回 (ok, 错误, 是否换了模型)。"""
        if ai not in AIS:
            return False, "未知 AI", False
        model, effort = (model or "").strip(), (effort or "").strip()
        if model and not agents.MODEL_RE.match(model):
            return False, "模型名不合法", False
        cur_model = model or agents.ROOM_DEFAULTS[ai][0]
        if effort and (not agents.EFFORT_RE.match(effort) or effort not in agents.effort_options(ai, cur_model)):
            return False, "effort 不在可选范围内", False
        with self.lock:
            if self.busy_for():
                return False, "AI 还在回复，等它们说完再改", False
            old_m, old_e = self.own_models(ai)
            if (old_m, old_e) == (model, effort):
                return True, "", False
            self.meta["models"][ai] = {"model": model, "effort": effort}
            self.save_meta()
            self.add_message("系统", "%s 换成了 %s" % (ai, self.model_label(ai)), quiet=True)
        return True, "", old_m != model

    # ---------- MCP / 技能 / 插件 ----------
    def tools_info(self, ai):
        return agents.tools_info(ai, self.meta["tools"][ai], self.workdir())

    def set_tools(self, ai, cfg):
        """改这场会议里某个 AI 的 MCP / 技能 / 插件开关。返回 (ok, 错误)。"""
        if ai not in AIS:
            return False, "未知 AI"
        new = agents.fix_tools(ai, cfg)
        with self.lock:
            if self.busy_for(ai):
                return False, "%s 还在回复，等它说完再改" % ai
            if new == self.meta["tools"][ai]:
                return True, ""
            self.meta["tools"][ai] = new
            self.save_meta()
            self.add_message("系统", "%s 的工具换了：%s" % (ai, agents.tools_summary(ai, new)), quiet=True)
        if ai == "codex":
            agents.stop_codex(self)   # 下一轮按新配置重起
        return True, ""

    # ---------- 模式 ----------
    def set_mode(self, mode):
        """切模式。返回 (ok, 错误)。"""
        if mode not in MODES:
            return False, "未知模式"
        with self.lock:
            if self.meta.get("mode") == mode:
                return True, ""
            if self.busy_for():
                return False, "AI 还在回复，等它们说完再切模式"
            self.meta["mode"] = mode
            self.state["chain"] = 0
            self.save_meta()
            self.add_message("系统", "切到干活模式" if mode == "work" else "切回讨论模式")
        agents.stop_codex(self)   # 两种模式加载的工具不一样，下一轮按新模式重起
        return True, ""

    # ---------- 干活模式：一步一步的改动记录、撤回 ----------
    def record_step(self, ai, pre_tree, edits, t0, t1):
        """AI 跑完后，按它自己的操作记录生成这一步的改动清单（只有记录里出现的文件才算）。有改动就记一步、插系统消息。"""
        ncmd = sum(1 for e in edits or [] if e.get("op") == "command")
        wd = self.workdir()
        try:
            top, _ = ws.git_dirs(wd)
            inside, outside = ws.split_edits(edits, top, wd)
            files = ws.build_files(wd, pre_tree, inside) if inside else []
        except Exception as e:
            self.add_message("系统", "改动记录生成失败：%s。%s 这一步改了什么看不到，也撤不回" % (e, ai))
            return None
        if not files and not outside and not ncmd:
            return None
        seq = None
        mine = {f["path"] for f in files}
        clash = {}
        for who, o in steps_in_repo(top):   # 时间上重叠、又改了同一个文件（这场会议的另一个 AI，或别的会议里的 AI）
            if (who == self.id and o["ai"] == ai) or o.get("undone") or o.get("t0") is None:
                continue
            if o["t0"] < t1 and t0 < o["t1"]:
                for f in o["files"]:
                    if f["path"] in mine:
                        clash.setdefault(f["path"], o["ai"] if who == self.id else "%s（会议「%s」）" % (o["ai"], room_title_of(who)))
        with self.lock:
            if files:
                self.meta["step_seq"] += 1
                seq = self.meta["step_seq"]
                ref = None
                try:
                    ref = ws.save_checkpoint(wd, self.ref_name(), seq, pre_tree)
                except Exception as e:
                    self.add_message("系统", "存档点引用没存上（%s），撤回仍可用，但 git gc 后可能失效" % e)
                self.meta["steps"][str(seq)] = {"v": 2, "ai": ai, "pre_tree": pre_tree, "ref": ref, "files": files,
                                                "outside": outside, "undone": False, "time": now_str(), "t0": t0, "t1": t1}
                self.save_meta()
        self.add_message("系统", ws.files_text(ai, files, outside, clash, ncmd), step=seq, not_for=ai)
        return seq

    def undo_plan(self, seq):
        """算出撤回这一步要动哪些文件（不动磁盘）。调用方持有 lock。返回 (ok, 错误, 计划)。
        冲突检查：记录里每个文件现在的 sha256 必须等于这一步记下的"改后"；对不上整步拒绝。"""
        st = self.meta["steps"].get(str(seq))
        if not st:
            return False, "没有这一步", None
        if st.get("undone"):
            return False, "这一步已经撤回过了", None
        if self.busy_for():
            return False, "AI 还在回复，等它们说完再撤回", None
        wd = self.workdir()
        try:
            top, _ = ws.git_dirs(wd)
        except Exception as e:
            return False, "撤回出错：%s" % e, None
        # 别的 AI（这场会议的另一个，或别的会议里的）时间上重叠、改了同一个文件：这一步的"改前"是自己开工那一刻拍的，
        # 不含对方同时写进去的东西，撤回会把对方的改动一起抹掉。不管哪一步先记下，只要对方那步还没撤，就整步拒绝
        mine = {f["path"] for f in st["files"] if not f.get("nb")}
        shared, who_set = [], set()
        if st.get("t0") is not None:
            for who, o in steps_in_repo(top):
                if o is st or (who == self.id and o["ai"] == st["ai"]) or o.get("undone") or o.get("t0") is None:
                    continue
                if o["t0"] < st["t1"] and st["t0"] < o["t1"]:
                    for f in o["files"]:
                        if f["path"] in mine and f["path"] not in shared:
                            shared.append(f["path"])
                            who_set.add(o["ai"] if who == self.id else "%s（会议「%s」）" % (o["ai"], room_title_of(who)))
        if shared:
            return False, "%s 同时也改过这些文件，撤回会把它的改动一起抹掉，不能撤：" % "、".join(sorted(who_set)) \
                + "、".join(shared[:8]) + ("…" if len(shared) > 8 else ""), None
        legacy = st.get("v") != 2   # 旧版（整体快照差异）的步：改后用 git blob sha1 比
        try:
            items, conflicts = [], []
            for f in st["files"]:
                full = ws.full_of(top, f["path"])
                if f.get("nb"):
                    items.append({"path": f["path"], "act": "撤不回"})
                    continue
                if f["post"] is None:
                    ok = not os.path.lexists(full)
                else:
                    ok = False
                    if os.path.isfile(full):
                        with open(full, "rb") as fh:
                            data = fh.read()
                        ok = (ws.blob_sha(data) if legacy else ws.sha256_of(data)) == f["post"]
                if not ok:
                    conflicts.append(f["path"])
                items.append({"path": f["path"], "act": "删除" if f["pre"] is None else "还原"})
        except Exception as e:
            return False, "撤回出错：%s" % e, None
        if conflicts:
            return False, "这些文件在这一步之后又被改过（不是这个 AI 这一步的结果了），不能撤：" + "、".join(conflicts[:8]) + ("…" if len(conflicts) > 8 else ""), None
        if not any(i["act"] != "撤不回" for i in items):
            return False, "这一步的文件都撤不回（被 git 忽略或不在存档范围）", None
        return True, "", {"ai": st["ai"], "items": items, "outside": st.get("outside") or []}

    def undo_preview(self, seq):
        """给确认框用：返回 (ok, 错误, 清单)。"""
        with self.lock:
            return self.undo_plan(seq)

    def undo_step(self, seq):
        """撤回某一步：只动这个 AI 这一步记录里的文件。返回 (ok, 错误)。"""
        with self.lock:
            ok, err, plan = self.undo_plan(seq)
            if not ok:
                return False, err
            st = self.meta["steps"][str(seq)]
            ai = st["ai"]
            wd = self.workdir()
            try:
                top, _ = ws.git_dirs(wd)
                todo = [f for f in st["files"] if not f.get("nb")]
                olds = {}
                for f in todo:
                    if f["pre"] is not None:
                        olds[f["path"]] = ws.git(["cat-file", "blob", f["pre"]], wd)
                for f in todo:
                    full = ws.full_of(top, f["path"])
                    if f["pre"] is None:
                        if os.path.lexists(full):
                            os.remove(full)
                        ws.remove_empty_dirs(full, top)
                    else:
                        os.makedirs(os.path.dirname(full), exist_ok=True)
                        tmp = full + ".rt_undo.tmp"
                        with open(tmp, "wb") as fh:
                            fh.write(olds[f["path"]])
                        os.replace(tmp, full)
            except Exception as e:
                return False, "撤回出错：%s" % e
            st["undone"] = True
            self.save_meta()
            nb = len(st["files"]) - len(todo)
        self.add_message("系统", "已撤回 %s 的这一步（%d 个文件%s）" % (ai, len(todo), "，另有 %d 个撤不回" % nb if nb else ""))
        return True, ""

    # ---------- 发言、排队、叫 AI ----------
    def job_tag(self, ai):
        return "%s#%d" % (self.id, self.state["epoch"][ai])

    def busy_for(self, ai=None):
        with self.lock:
            if ai is None:
                return bool(self.state["running"] or self.pending or self.state["compacting"])
            return ai in self.state["running"] or ai in self.pending or ai in self.state["compacting"]

    def queued_count(self):
        """排着还没开始的：等着回话的 AI + 等着压缩的 AI。"""
        return len(self.pending) + len([a for a in self.state["compacting"] if "compact:" + a not in self.state["running"]])

    def enqueue(self, ai, user=False):
        """排上一个 AI 的回复。已排上还没开始的不重复排（它开始时才取增量，自然会看到新消息）。
        user=True（用户发言触发）：立刻开线程，和另一个 AI 并行；
        user=False（AI 之间的对话）：进这场会议的串行队列，一个跑完下一个才开始。两种都要等自己 AI 的锁（同一个 AI 永远串行）。"""
        with self.lock:
            # 用户发话不能被串行队列挡住：它已经排在 AI 之间的队列里（前面可能是别人的长活）时，照样马上另开一个并行的。
            # 队列里那份轮到时发现该看的都看过了，会自己跳过。
            if ai in self.pending and not user:
                return
            self.pending.append(ai)
            tag = self.job_tag(ai)
            if user:
                threading.Thread(target=self.run_job, args=(ai, tag, True), daemon=True).start()
            else:
                self.jobs.put((tag, "reply", ai))

    def make_reply(self, mid, part=None):
        """用户引用的那条：只认会议里真有的、人或 AI 说的话；摘一段原文（太长截断）。part = 用户在那条里选中的一段（右键引用），有就只引这段。"""
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            return None
        with self.lock:
            if not 0 <= mid < len(self.messages):
                return None
            m = self.messages[mid]
        if m["speaker"] not in ("你",) + AIS:
            return None
        t = re.sub(r"(\*\*|__|~~|`+|^#+\s*|^\s*[-*>]\s+)", "", m.get("text") or "", flags=re.M)   # 摘要里不要 markdown 符号
        t = " ".join(t.split())
        part = " ".join(str(part or "").split())
        if part:
            return {"id": mid, "speaker": m["speaker"], "part": True,
                    "text": part[:QUOTE_LEN] + ("…" if len(part) > QUOTE_LEN else "")}
        if not t:
            t = "（附件）" if m.get("attachments") else ""
        return {"id": mid, "speaker": m["speaker"], "text": t[:QUOTE_LEN] + ("…" if len(t) > QUOTE_LEN else "")}

    def handle_message(self, speaker, text, attachments=None, origin="room", reply_to=None):
        """记录一条消息并按 @ 路由。origin：room = 用户或会议室自己叫起来的 AI；external = 会议室外的 claude/codex（/api/post、MCP）。"""
        external = origin == "external"
        with self.lock:
            if self.closed:
                return False
            cmd = text.strip().lower().split()
            if speaker == "你" and not attachments and cmd and cmd[0] == "/compact" and len(cmd) <= 2 and set(cmd[1:]) <= set(AIS):
                self.request_compact(cmd[1:] or AIS)
                return True
            if speaker == "你":
                names = mentions(text)
                # 没 @ 人、引用的是某个 AI 的话：就是回它，只叫它
                quoted = reply_to["speaker"] if reply_to and reply_to["speaker"] in AIS else None
                targets = [n for n in names if n in AIS] or ([quoted] if quoted else list(AIS))
                to_user = False
            elif speaker in AIS:
                # external 的 @自己 也要叫会议室里的同名 AI，所以不排除
                names = mentions(text, exclude=None if external else speaker)
                targets = [n for n in names if n in AIS]
                to_user = "用户" in names
            else:
                targets, to_user = [], False
            changes, send_errs = [], []
            if speaker in AIS:
                text, changes = self.apply_board_cmds(speaker, text)
                text, sent, send_errs = self.apply_send_cmds(text)
                attachments = (attachments or []) + sent
            if text or attachments or not (changes or send_errs):
                self.add_message(speaker, text, attachments, to_user, origin="external" if external else None, reply_to=reply_to)
            self.board_note(speaker, changes, not_for=None if external else speaker)
            self.send_note(speaker, send_errs)
            if speaker == "你":
                self.state["chain"] = 0
            for t in targets:
                if self.meta["handed_off"][t]:
                    self.system_note("%s 现在在终端里，回来后会补看" % t)
                else:
                    self.enqueue(t, user=(speaker == "你"))
        return True

    def post_progress(self, ai, text, atts):
        """AI 一轮中途经 bridge 发进来的一句进度 / 文件：照登，但里面的 @ 不叫人（它这一轮还没说完）。"""
        text = (text or "").strip()
        if not text and not atts:
            return None
        return self.add_message(ai, text, atts or None, to_user="用户" in mentions(text, exclude=ai))

    def push_live(self, ai, res):
        """AI 一轮里中途说的话（claude 的文字块、codex 的 commentary）放进 live，页面状态行显示最近一句；不进会议记录。"""
        t = res.pop("live", None)
        if t and ai in self.live:
            t = t.strip()
            self.live[ai]["text"] = t if len(t) <= 400 else t[:400] + "…"
            self.live[ai]["time"] = datetime.now().strftime("%H:%M:%S")

    def run_job(self, ai, tag, user):
        try:
            self.process_job(ai, tag, user)
        except Exception as e:
            self.add_message("系统", "内部错误：%s" % e)

    def _worker(self):
        while True:
            item = self.jobs.get()
            if item is None:
                return
            tag, kind, ai = item
            try:
                self.process_job(ai, tag, False)
            except Exception as e:
                self.add_message("系统", "内部错误：%s" % e)

    def process_job(self, ai, tag, user=False):
        """叫一次 AI。整个过程持有这个 AI 自己的锁（同一个 AI 永远串行）；增量在拿到锁之后才取快照。"""
        with self.ai_locks[ai]:
            with self.lock:
                if ai in self.pending:
                    self.pending.remove(ai)
                forced = ai in self.state["force_work"]   # 先取走：下面提前返回时也不能留到下一轮
                self.state["force_work"].discard(ai)
                if self.closed or tag != self.job_tag(ai) or self.meta["handed_off"][ai]:
                    return
                if not user and self.meta.get(ai + "_session") and not self.delta_for(ai)[0]:
                    return   # AI 之间的对话排到它时，该看的它已经看过了，不白叫
                self.state["running"].append(ai)
                self.live[ai] = {"text": "", "time": "", "since": time.time()}
            try:
                mode = "work" if forced else self.mode()
                wd = self.workdir()
                pre = None
                t0 = time.time()
                if mode == "work" and not ws.in_git(wd):
                    with self.lock:
                        warn = not self.meta.get("nogit_warned")
                        self.meta["nogit_warned"] = True
                        self.save_meta()
                    if warn:
                        self.add_message("系统", "工作文件夹 %s 不是 git 仓库：干活模式照常改，但改了什么记不下来，也撤不回" % wd)
                elif mode == "work":
                    try:
                        pre = ws.snapshot_tree(wd)   # 只用来取"改前"原文件；改了哪些文件以 AI 自己的操作记录为准
                    except Exception as e:
                        self.add_message("系统", "存档点拍不出来：%s。干活模式下不叫 %s" % (e, ai))
                        return
                prompt, upto, ver, imgs = self.build_prompt(ai, mode)
                sid = self.meta.get(ai + "_session")
                try:
                    res = ai_caller(ai, prompt, sid, imgs, mode, self)   # 明着传：落盘那一轮是强制干活模式，跟会议的模式不一样
                except BaseException:
                    if pre is not None:
                        self.add_message("系统", "%s 调用中途崩了，这一步改了什么没能记录" % ai)
                    raise
                ok, out, new_sid = res[0], res[1], res[2]
                with self.lock:
                    was_stopped = ai in self.state["stopped"]
                    self.state["stopped"].discard(ai)
                if was_stopped and not ok:
                    ok, out = False, "%s 被你打断了" % ai
                edits = res[3] if len(res) > 3 else []
                if ok:
                    with self.lock:
                        self.meta[ai + "_session"] = new_sid
                        self.meta["last_seen"][ai] = upto
                        self.meta["announcement_seen"][ai] = ver
                        self.meta["mode_seen"][ai] = mode_key(mode)
                        self.meta["returned"][ai] = False
                        if ai in self.last_ctx:
                            self.meta["ctx"][ai] = self.last_ctx.pop(ai)
                        self.save_meta()
                        self.state["chain"] += 1
                    if was_stopped:   # 刚好在"停"的时候说完：话照登，但里面的 @ 不再叫人
                        rest, ch = self.apply_board_cmds(ai, out)
                        rest, sent, errs = self.apply_send_cmds(rest)
                        if rest or sent or not (ch or errs):
                            self.add_message(ai, rest, sent, to_user="用户" in mentions(rest, exclude=ai))
                        self.board_note(ai, ch, not_for=ai)
                        self.send_note(ai, errs)
                    else:
                        self.handle_message(ai, out)
                else:
                    self.add_message("系统", out)
                if pre is not None:
                    self.record_step(ai, pre, edits, t0, time.time())
            finally:
                with self.lock:
                    if ai in self.state["running"]:
                        self.state["running"].remove(ai)
                    self.live.pop(ai, None)
                guard.release_turn((self.id, ai))

    # ---------- 压缩 ----------
    def request_compact(self, targets=AIS):
        """每个 AI 各压各的：各开一个线程，只等它自己的锁，另一个 AI 照常能叫。返回这次排上的 AI（已在压的不重复排）。"""
        with self.lock:
            todo = [a for a in AIS if a in targets and a not in self.state["compacting"]]
            self.state["compacting"].update(todo)
            if todo:
                self.system_note("开始压缩：" + "、".join(todo))
        for a in todo:
            threading.Thread(target=self.process_compact, args=(a,), daemon=True).start()
        return todo

    def compact_one(self, ai):
        """压缩一个 AI 的会话。返回给人看的结果描述。"""
        with self.lock:
            sid = self.meta.get(ai + "_session")
            handed = self.meta["handed_off"][ai]
        if handed:
            return "%s 在终端里，没压" % ai
        if not sid:
            return "%s 还没建会话，不用压" % ai
        if ai_native_compact(ai, sid, self):
            with self.lock:
                self.meta["ctx"][ai] = None   # 压完多少不知道，下一轮回话后再显示
                self.save_meta()
            return "%s 压缩好了" % ai
        if ai == "codex":   # codex 不走"摘要换新会话"：压不了就直说，它快满时自己也会自动压
            if agents.CODEX_BACKEND == "exec":
                return "codex（exec 方式）不支持手动压缩，它快装不下时会自己压"
            return "codex 没压成（没看到压缩记录），会话没动"
        # 退化：让它写摘要，再新建会话，第一条消息是前言 + 目标 + 摘要
        d, upto = self.delta_for(ai)
        body = "\n\n".join(fmt_message(m) for m in d)
        ask = ("会议室要压缩上下文了。请把这场会议到目前为止的要点写成一份摘要："
               "已定的结论、没解决的问题、各人的立场、谁答应了要做什么、重要的文件路径。"
               "不要 @ 任何人，直接输出摘要。\n\n" + ("还没递给你的新消息：\n" + body if body else "（没有新消息）"))
        r = ai_caller(ai, ask, sid, (), None, self)
        ok, summary = r[0], r[1]
        if not ok:
            return "%s 压缩失败（写摘要出错：%s）" % (ai, summary.splitlines()[0] if summary else "")
        with self.lock:
            ver = self.meta["ann_ver"]
            mode = self.mode()
        first = "\n\n".join([preamble(mode, self.workdir()), "这场会议的目标：\n" + self.goal_text(), "看板：\n" + self.board_text(),
                             "这场会议到目前为止的摘要：\n" + summary.strip() + "\n\n以上是这场会议之前的摘要。",
                             "收到后只回复“收到”两个字。"])
        r2 = ai_caller(ai, first, None, (), None, self)
        ok, new_sid = r2[0], r2[2]
        if not ok or not new_sid:
            return "%s 压缩失败（新建会话出错）" % ai
        with self.lock:
            if sid and sid != new_sid:
                self.meta["old_sessions"][ai].append(sid)   # 摘要续接换了新会话；旧的留着记录，删会议时一起删
            self.meta[ai + "_session"] = new_sid
            self.meta["last_seen"][ai] = upto
            self.meta["announcement_seen"][ai] = ver
            self.meta["mode_seen"][ai] = mode_key(mode)
            self.meta["returned"][ai] = False
            self.meta["ctx"][ai] = self.last_ctx.pop(ai, None)
            self.save_meta()
        return "%s 续接（摘要 + 新会话）" % ai

    def process_compact(self, ai):
        """压一个 AI：等它手上的活干完（它自己的锁），压完插一条系统消息。"""
        try:
            with self.ai_locks[ai]:
                with self.lock:
                    if self.closed:
                        return
                    self.state["running"].append("compact:" + ai)
                try:
                    r = self.compact_one(ai)
                except Exception as e:
                    r = "%s 压缩出错：%s" % (ai, e)
                finally:
                    with self.lock:
                        if "compact:" + ai in self.state["running"]:
                            self.state["running"].remove("compact:" + ai)
            self.system_note(r)
        finally:
            with self.lock:
                self.state["compacting"].discard(ai)

    # ---------- 落盘 ----------
    def wrapup_text(self):
        if os.path.isfile(os.path.join(self.workdir(), ".claude", "skills", "wrapup", "SKILL.md")):
            return WRAPUP_TEXT
        return WRAPUP_GENERIC.replace("{wd}", self.workdir())

    def request_wrapup(self):
        """叫 claude 按 wrapup 落盘并提交推送；这一轮强制干活模式。返回 (ok, 错误)。"""
        with self.lock:
            if self.meta["handed_off"]["claude"]:
                return False, "claude 现在在终端里，先收回再落盘"
            if self.busy_for("claude"):
                return False, "claude 还在回复或排队，等它说完再落盘"
            self.state["force_work"].add("claude")
            self.add_message("系统", self.wrapup_text(), not_for="codex")
            self.enqueue("claude", user=True)
        return True, ""

    # ---------- 停 ----------
    def stop_ais(self, targets=AIS):
        """用户按"停"：targets 里正在跑的 AI 连同它起的命令（git push 之类）一起杀掉，它们排着的全部作废；别的 AI、别的会议不受影响。
        返回给人看的描述。"""
        targets = [a for a in targets if a in AIS]
        with self.lock:
            for a in targets:
                self.state["epoch"][a] += 1     # 已经排上的（队列里、等锁的）开跑时对不上计数，自己放弃
            self.pending[:] = [a for a in self.pending if a not in targets]
            keep = []
            while True:
                try:
                    item = self.jobs.get_nowait()
                except queue.Empty:
                    break
                if item is None or item[1] != "reply" or item[2] not in targets:
                    keep.append(item)       # 压缩、别的 AI 的不归这次"停"管
            for item in keep:
                self.jobs.put(item)
            running = [a for a in self.state["running"] if a in targets]
            self.state["stopped"].update(running)
            for a in targets:
                self.state["force_work"].discard(a)
        for a in running:
            p = self.procs.get(a)
            if p is not None and p.poll() is None:
                kill_tree(p)
            if a == "codex":
                agents.stop_codex(self)   # 杀掉这场会议的 app-server 进程树；下一轮自动重起，会话从记录里接上
        who = "、".join(targets)
        if running:
            msg = "你强行打断了：%s。%s排着的也清掉了。" % ("、".join(running), who)
            msg += "已经改了的文件不会自动还原，干活模式下可以按步骤撤回；起的命令（比如 git push）也一起停了，可能只做了一半。"
        else:
            msg = "你按了停（%s）：排着的都清掉了。" % who
        self.add_message("系统", msg)
        return msg

    # ---------- 交给终端 ----------
    def set_handoff(self, ai, on):
        """返回 (ok, 错误或提示)。"""
        if ai not in AIS:
            return False, "未知 AI"
        with self.lock:
            if on == self.meta["handed_off"][ai]:
                return True, ""
            if on:
                if not self.meta.get(ai + "_session"):
                    return False, "还没叫过它，先在会议里 @ 它一次"
                if self.busy_for(ai):
                    return False, "它正在跑或排着队，等它说完再交"
                self.meta["handed_off"][ai] = True
                self.save_meta()
                self.system_note("%s 去终端了" % ai)
            else:
                self.meta["handed_off"][ai] = False
                self.meta["returned"][ai] = True
                self.save_meta()
                self.system_note("%s 回到会议室" % ai)
        return True, ""

    def handoff_cmd(self, ai):
        sid = self.meta.get(ai + "_session")
        if not sid:
            return None
        return "cd \"%s\"; %s" % (self.workdir(), "claude --resume " + sid if ai == "claude" else "codex resume " + sid)

    # ---------- 给页面 ----------
    def summary(self):
        """会议列表里一行：标题、状态、谁在跑、消息数、最后一条 → 用户 的编号。"""
        with self.lock:
            last_tu = next((m["id"] for m in reversed(self.messages) if m.get("to_user")), -1)
            return {"id": self.id, "title": self.meta["title"], "created": self.meta.get("created", ""),
                    "status": self.meta.get("status", "open"), "archived": is_archived(self.meta.get("status")),
                    "workdir": self.workdir(), "mode": self.mode(), "loaded": True,
                    "running": [a for a in self.state["running"]], "queued": self.queued_count(),
                    "count": len(self.messages), "last_to_user": last_tu,
                    "last_time": self.messages[-1]["time"] if self.messages else ""}

    def api_state(self):
        with self.lock:
            ais = {}
            for a in AIS:
                ais[a] = {"session": bool(self.meta.get(a + "_session")), "handed_off": self.meta["handed_off"][a],
                          "busy": self.busy_for(a), "compacting": a in self.state["compacting"], "cmd": self.handoff_cmd(a),
                          "model": self.model_info(a), "ctx": self.meta["ctx"].get(a),
                          "tools": agents.tools_summary(a, self.meta["tools"][a])}
            return {"meeting": self.id, "title": self.meta["title"], "status": self.meta["status"],
                    "archived": is_archived(self.meta["status"]),
                    "announcement": self.meta["announcement"], "ann_ver": self.meta["ann_ver"], "ais": ais,
                    "board": self.meta["board"], "workdir": self.workdir(), "default_workdir": PROJECT, "stop_each": True,
                    "mode": self.mode(),
                    "steps_undone": [int(k) for k, v in self.meta["steps"].items() if v.get("undone")],
                    "running": list(self.state["running"]), "queued": self.queued_count(),
                    "claims": [c for c in guard.listing() if c["room"] == self.id or in_dir(c["shown"], self.workdir())],
                    "live": {a: {"text": v["text"], "time": v["time"], "secs": int(time.time() - v["since"])} for a, v in list(self.live.items())}}

    def messages_since(self, since):
        with self.lock:
            return {"meeting": self.id, "messages": self.messages[since:], "next": len(self.messages),
                    "running": list(self.state["running"]), "queued": self.queued_count()}


# ===================== 会议目录的增删 =====================
def scan(base):
    out = []
    if os.path.isdir(base):
        for name in os.listdir(base):
            p = os.path.join(base, name, "meeting.json")
            if not os.path.isfile(p):
                continue
            m = read_json(p)
            if not isinstance(m, dict):
                continue
            st = m.get("status", "closed")
            out.append({"id": name, "title": m.get("title", name), "created": m.get("created", ""),
                        "status": st, "archived": is_archived(st), "workdir": m.get("workdir") or PROJECT,
                        "mode": m.get("mode", "discuss")})
    return out


_log_cache = {}   # 会议目录 -> ((大小, 修改时间), 条数, 最后一条 → 用户 的编号, 最后一条的时间)


def log_stats(d):
    p = os.path.join(d, "log.jsonl")
    try:
        st = os.stat(p)
    except OSError:
        return 0, -1, ""
    sig = (st.st_size, st.st_mtime)
    hit = _log_cache.get(d)
    if hit and hit[0] == sig:
        return hit[1:]
    n, last_tu, last_t = 0, -1, ""
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                x = json.loads(line)
            except Exception:
                continue
            if x.get("to_user"):
                last_tu = n
            last_t = x.get("time", "")
            n += 1
    _log_cache[d] = (sig, n, last_tu, last_t)
    return n, last_tu, last_t


def unique_name(base_dir, name):
    new, i = name, 1
    while os.path.exists(os.path.join(base_dir, new)):
        i += 1
        new = "%s_%d" % (name, i)
    return new


def meeting_sessions(m):
    """一场会议用过的全部会话 id：{ai: [id, ...]}（当前的 + 压缩续接换下来的旧的）。"""
    out = {}
    for a in AIS:
        ss = [m.get(a + "_session")] + list(((m.get("old_sessions") or {}).get(a)) or [])
        out[a] = [v for i, v in enumerate(ss) if isinstance(v, str) and SID_RE.match(v) and v not in ss[:i]]
    return out


class Rooms:
    """读进内存的会议（打开过的、外部发过言的）。会议在这里才会跑 AI；没读进来的只是磁盘上的目录。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.loaded = {}

    def get(self, rid):
        """读进来（已经在就直接给）。没有这场会议返回 None。"""
        if not rid or not ROOM_RE.match(rid) or os.path.basename(rid) != rid or rid in (".", ".."):
            return None
        with self.lock:
            r = self.loaded.get(rid)
            if r is not None:
                return r
            d = os.path.join(MEET_DIR, rid)
            m = read_json(os.path.join(d, "meeting.json"))
            if not isinstance(m, dict):
                return None
            msgs = []
            jl = os.path.join(d, "log.jsonl")
            if os.path.exists(jl):
                with open(jl, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                x = json.loads(line)
                                x["id"] = len(msgs)
                                msgs.append(x)
                            except Exception:
                                pass
            r = Room(rid, d, fix_meta(m), msgs)
            self.loaded[rid] = r
            return r

    def all(self):
        with self.lock:
            return list(self.loaded.values())

    def create(self, title, ann="", wd=None):
        """建一场新的。别的会议照常跑，不受影响。返回 Room。"""
        now = datetime.now()
        title = (title or "").strip() or "会议"
        with self.lock:
            os.makedirs(MEET_DIR, exist_ok=True)
            name = unique_name(MEET_DIR, "%s_%s" % (now.strftime("%Y-%m-%d_%H%M"), safe_title(title)))
            d = os.path.join(MEET_DIR, name)
            os.makedirs(os.path.join(d, "files"))
            meta = default_meta(title, ann, wd)
            write_json(os.path.join(d, "meeting.json"), meta)
            with open(os.path.join(d, "log.md"), "w", encoding="utf-8") as f:
                f.write("# %s\n\n创建于 %s\n\n" % (title, meta["created"]))
            open(os.path.join(d, "log.jsonl"), "a").close()
            r = Room(name, d, meta, [])
            self.loaded[name] = r
        set_last(name)
        return r

    def unload(self, rid):
        with self.lock:
            r = self.loaded.pop(rid, None)
        if r is not None:
            r.close()
        return r

    def listing(self):
        """全部会议（不含回收站），带状态。归档的沉到底部。"""
        with self.lock:
            loaded = dict(self.loaded)
        out = []
        for x in scan(MEET_DIR):
            r = loaded.get(x["id"])
            if r is not None:
                out.append(r.summary())
                continue
            n, last_tu, last_t = log_stats(os.path.join(MEET_DIR, x["id"]))
            x.update({"loaded": False, "running": [], "queued": 0, "count": n, "last_to_user": last_tu, "last_time": last_t})
            out.append(x)
        out.sort(key=lambda x: (x["created"], x["id"]), reverse=True)
        out.sort(key=lambda x: x["archived"])   # 稳定排序：归档的沉到底部
        return out

    def busy_rooms(self):
        return [r for r in self.all() if r.busy_for()]


rooms = Rooms()


def room_title_of(rid):
    r = rooms.loaded.get(rid)
    if r is not None:
        return r.title()
    m = read_json(os.path.join(MEET_DIR, rid, "meeting.json")) or {}
    return m.get("title") or rid


def steps_in_repo(top):
    """同一个仓库里所有会议记下的步（读进内存的用内存里的，别的读 meeting.json）。返回 [(会议号, 步)]。"""
    out, seen = [], set()
    for r in rooms.all():
        seen.add(r.id)
        if in_dir(r.workdir(), top):
            with r.lock:
                out += [(r.id, s) for s in r.meta["steps"].values()]
    for x in scan(MEET_DIR):
        if x["id"] in seen or not in_dir(x["workdir"], top):
            continue
        m = read_json(os.path.join(MEET_DIR, x["id"], "meeting.json")) or {}
        out += [(x["id"], s) for s in (m.get("steps") or {}).values() if isinstance(s, dict)]
    return out


def last_room():
    try:
        with open(LAST_FILE, encoding="utf-8") as f:
            rid = f.read().strip()
    except OSError:
        return None
    return rid if rid and os.path.isfile(os.path.join(MEET_DIR, rid, "meeting.json")) else None


def set_last(rid):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LAST_FILE, "w", encoding="utf-8") as f:
            f.write(rid or "")
    except OSError:
        pass


def set_archived(rid, on):
    """归档 / 取消归档：只改标记。返回 (ok, 错误)。"""
    if rid not in [x["id"] for x in scan(MEET_DIR)]:
        return False, "没有这场会议"
    st = "archived" if on else "open"
    r = rooms.loaded.get(rid)
    if r is not None:
        with r.lock:
            r.meta["status"] = st
            r.save_meta()
    else:
        p = os.path.join(MEET_DIR, rid, "meeting.json")
        m = read_json(p) or {}
        m["status"] = st
        write_json(p, m)
    return True, ""


def _tag_ref_name(d, orig):
    """目录改名前，把存档点引用用的名字记在 meeting.json 里，彻底删除时才找得到。"""
    p = os.path.join(d, "meeting.json")
    m = read_json(p) or {}
    if not m.get("ref_name"):
        m["ref_name"] = ref_safe(orig)
        write_json(p, m)


def _move_meeting(src_base, dst_base, name):
    """name 必须是 src_base 服务端列表里的目录名；两头都要落在各自目录内。返回新目录名。"""
    if os.path.basename(name or "") != name or name not in [x["id"] for x in scan(src_base)]:
        raise ValueError("没有这场会议")
    src = os.path.join(src_base, name)
    if not in_dir(src, src_base) or norm(os.path.dirname(src)) != norm(src_base):
        raise ValueError("路径不安全")
    os.makedirs(dst_base, exist_ok=True)
    new = unique_name(dst_base, name)
    dst = os.path.join(dst_base, new)
    if not in_dir(dst, dst_base):
        raise ValueError("路径不安全")
    _tag_ref_name(src, name)
    shutil.move(src, dst)
    return new


def trash_meeting(rid):
    """移进回收站。这场会议的 AI 在跑时不行（别的会议在跑没关系）。返回 (ok, 错误)。"""
    with rooms.lock:
        r = rooms.loaded.get(rid)
        if r is not None and r.busy_for():
            return False, "这场会议的 AI 还在回复，等它们说完再删"
        if rid not in [x["id"] for x in scan(MEET_DIR)]:
            return False, "没有这场会议"
        if r is not None:
            rooms.unload(rid)
        else:
            guard.release_room(rid)
        try:
            _move_meeting(MEET_DIR, TRASH_DIR, rid)
        except Exception as e:
            return False, str(e)
    if last_room() is None:
        set_last("")
    return True, ""


def list_trash():
    out = scan(TRASH_DIR)
    out.sort(key=lambda x: (x["created"], x["id"]), reverse=True)
    return out


def restore_meeting(name):
    with rooms.lock:
        try:
            new = _move_meeting(TRASH_DIR, MEET_DIR, name)
        except Exception as e:
            return False, str(e), None
    return True, "", new


def purge_all():
    """清空回收站。返回 (ok, 错误)。"""
    for n in [x["id"] for x in scan(TRASH_DIR)]:
        ok, msg = purge_meeting(n)
        if not ok:
            return False, msg
    return True, ""


def purge_meeting(name):
    """彻底删除回收站里的一场。返回 (ok, 错误)。"""
    with rooms.lock:
        item = next((x for x in scan(TRASH_DIR) if x["id"] == name), None)
        if os.path.basename(name or "") != name or not item:
            return False, "回收站里没有这场会议"
        d = os.path.join(TRASH_DIR, name)
        if not in_dir(d, TRASH_DIR) or norm(os.path.dirname(d)) != norm(TRASH_DIR):
            return False, "路径不安全"
        m = read_json(os.path.join(d, "meeting.json")) or {}
        ref_name = m.get("ref_name") or ref_safe(name)
        wd = m.get("workdir") or PROJECT
        sids = meeting_sessions(m)
        shutil.rmtree(d)
        try:
            if os.path.isdir(wd):
                ws.purge_refs(ref_name, wd)
        except Exception:
            pass
        # 别的会议（还在的、回收站里剩下的）也在用的会话不删
        used = set()
        for base in (MEET_DIR, TRASH_DIR):
            for x in scan(base):
                mm = read_json(os.path.join(base, x["id"], "meeting.json")) or {}
                for a, ss in meeting_sessions(mm).items():
                    used.update((a, v) for v in ss)
        sids = {a: [v for v in ss if (a, v) not in used] for a, ss in sids.items()}
    if any(sids.values()):
        threading.Thread(target=delete_sessions, args=(m.get("title") or name, sids), daemon=True).start()
    return True, ""


def delete_sessions(title, sids):
    """彻底删除会议后在后台删两个 AI 的会话，结果记一条系统消息（记在最近打开的那场会议里，只给人看）。"""
    parts = []
    for sid in sids.get("claude", []):
        n = agents.delete_claude_session(sid)
        parts.append("claude 会话 %s…：%s" % (sid[:8], "删了 %d 项" % n if n else "本机没找到"))
    for sid in sids.get("codex", []):
        ok, why = agents.delete_codex_thread(sid)
        parts.append("codex 会话 %s…：%s" % (sid[:8], ("删了" + ("（%s）" % why if why else "")) if ok else "没删成（%s）" % why))
    lr = last_room()
    r = rooms.get(lr) if lr else None
    if r is not None:
        r.system_note("彻底删除「%s」：%s" % (title, "；".join(parts)))
