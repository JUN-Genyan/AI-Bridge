# -*- coding: utf-8 -*-
"""AI Bridge 会议室：用户 + Claude Code + Codex。以会议为单位，每个 AI 一个固定会话。
只用标准库，只绑 127.0.0.1。"""
import atexit, json, os, re, shutil, subprocess, tempfile, threading, queue, webbrowser, mimetypes, uuid, glob, hashlib, time, secrets, hmac
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, parse_qs, quote

HOST, PORT = "127.0.0.1", int(os.environ.get("ROUNDTABLE_PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.environ.get("ROUNDTABLE_LOG_DIR", os.path.join(HERE, "data"))   # 会议记录、附件、私有 index；不进 git
# 默认工作文件夹：老会议没记工作文件夹时用它，新会议弹窗没东西可带时也用它
PROJECT = os.path.normpath(os.environ.get("ROUNDTABLE_DEFAULT_DIR") or os.path.expanduser("~"))


# 附件路径相对谁记：会议记录目录在本项目里（默认）就相对项目根（老记录都是这么存的），不在就相对会议记录目录（跨盘也不出错）
ATT_BASE = PROJECT if os.path.normcase(os.path.abspath(LOG_DIR)).startswith(os.path.normcase(os.path.abspath(PROJECT)) + os.sep) else LOG_DIR


def workdir():
    """当前会议的工作文件夹：两个 AI 在这里跑、git 存档 / 撤回也在这里。开会时定死（claude 的会话按文件夹存，中途换了接不上）。"""
    return ((meta or {}).get("workdir")) or PROJECT
MEET_DIR = os.path.join(LOG_DIR, "meetings")
CUR_FILE = os.path.join(LOG_DIR, "current.txt")
MAX_BODY = 64 * 1024
MAX_UPLOAD = 25 * 1024 * 1024
MAX_ANN = 16 * 1024
MAX_ATTACH = 10
MAX_IMAGES = 5
RT_TOKEN = secrets.token_urlsafe(24)   # 每次启动随机；页面加载时注入 HTML，前端每个请求带 X-RT-Token
# 外部程序经 /api/post 只能以 claude / codex 身份发言（会议室外的同名 AI，消息带 origin=external）。
# 你 / 系统 不在内：你 = 只能来自网页上用户的操作；系统 = 会议室自己
EXTERNAL_SPEAKERS = ("claude", "codex")
USER_ONLY_MSG = "这个接口只给网页上的用户用；外部程序请用 /api/post 并写明自己的身份"
TIMEOUT = 600
TIMEOUT_WORK = 1800   # 干活模式完全放开后，提交 / 推送大文件这类活十分钟不够
MODES = ("discuss", "work")
MODE_NAME = {"discuss": "讨论模式", "work": "干活模式"}

PREAMBLE_BASE = (
    "这是一个三方会议室，参与者是用户、claude（Claude Code）、codex（Codex），"
    "工作目录是 {wd}。回答简短直接，用大白话。\n"
    "会议记录里署名 claude / codex 的话，有些是在会议室外（用户桌面或终端里）的同名 AI 说的，你没说过的就当是它说的，接着讨论即可；"
    "只有署名“你”的才是用户本人。\n"
    "说话必须 @ 对象：对用户说就写 @用户，对另一个 AI 说就写 @claude 或 @codex。"
    "只在真需要对方接话时才 @ 另一个 AI，避免来回空转；@ 自己没有用。\n"
    "用户没 @ 任何人时，claude 和 codex 两个都会被叫。"
    "用户发言时你和另一个 AI 是同时回答的，看不到对方这一轮的回答；回完后如果发现对方说的跟你冲突或需要补充，再 @ 它。\n"
    "默认先简单聊天：短答，不主动查文件、不调工具；用户要深入、要核对或要干活时再查。\n"
    "你在这场会议里有一个固定的会话：之后每次叫你，只会递给你自上次以来的新消息（格式 [发言人] 正文），"
    "之前的内容你自己记得。\n"
    "附件给的是本机路径，用读文件工具（Read）去看，图片也可以直接读。"
)

MODE_DISCUSS = (
    "【当前是讨论模式】你只能看文件，不能改任何东西；要改的话让用户切到干活模式，或回各自的会话去做。"
    "codex 注意：你的 MCP 工具被关了；命令行能用来读文件，但在只读沙箱里，写不进任何东西。"
)

MODE_WORK = (
    "【当前是干活模式】你有完整权限，跟用户平时在桌面版 / 终端里用你一样：能改文件、跑命令、联网、用 git、用技能。"
    "只有 MCP 工具没加载（为了回得快），要用 Blender / Godot / ComfyUI 这类 MCP 的活，让用户去桌面版做。规矩：\n"
    "1. 动手前先 @ 对方说清要改哪些文件，别和对方同时改同一个文件。\n"
    "2. 删文件、git commit / push / reset、装软件、动项目外的东西，这类撤不回的事先在会议里说清楚要做什么，用户没反对再做。\n"
    "3. 会议室按你自己的写文件操作（claude 的 Edit/Write，codex 的 apply_patch）记录每一步改了哪些文件，用户能一键撤回；"
    "用命令行写的文件不在记录里，撤不回。能用编辑工具改的就别用命令写。\n"
    "4. 改完 @用户 简短汇报：改了什么、哪些文件。"
)


BOARD_RULES = (
    "看板：会议室里有一块看板（公告 + 待办），用户和你们都能写。要写就在回复里单独起一行：\n"
    "【公告】内容 —— 贴一条公告；【待办】内容 —— 加一条待办；"
    "【完成】编号 —— 勾掉待办（几个编号用空格隔开）；【删】编号 —— 删掉一条。\n"
    "这几行会被会议室收走，不显示在聊天里，改动会以系统消息告诉大家。只记真要跟踪的事，别刷屏。"
)


def mode_text(mode):
    return (MODE_WORK if mode == "work" else MODE_DISCUSS) + "\n" + BOARD_RULES


def mode_key(mode):
    """模式 + 规矩文本的指纹。规矩改了（比如干活模式放开），已有会话下一轮也会收到新版。"""
    return mode + ":" + hashlib.sha1(mode_text(mode).encode("utf-8")).hexdigest()[:8]


def timeout_for(mode):
    return TIMEOUT_WORK if mode == "work" else TIMEOUT


def preamble(mode="discuss"):
    return PREAMBLE_BASE.replace("{wd}", workdir()) + "\n" + mode_text(mode)

lock = threading.RLock()
meta = None            # 当前会议的 meeting.json 内容；没有会议时为 None
mdir = None            # 当前会议目录（完整路径）
messages = []          # {id, speaker, text, time, attachments?, to_user?, quiet?}
jobs = queue.Queue()   # 全局串行队列：(会议id, 类型, ai)  类型：reply（AI 之间的对话） / compact
pending = []           # 已排上（队列里或在等自己的锁）还没开始的 ai，避免同一个人被重复叫
ai_locks = {"claude": threading.Lock(), "codex": threading.Lock()}   # 每个 AI 只有一个会话，同一个 AI 永远串行
state = {"running": [], "chain": 0, "compact_q": 0,   # running：正在跑的 ai 名字列表（压缩时含 "compact"）
         "force_work": set(),                         # 下一轮不管会议在什么模式都按干活模式叫的 AI（落盘用）
         "epoch": {"claude": 0, "codex": 0},          # 停了谁就给谁加一；排队时记下，开跑时对不上就不跑
         "stopped": set()}                            # 被"停"打断、还没收尾的 AI
live_procs = {}   # ai -> 正在跑的子进程（claude -p / 旧的 codex exec），"停"的时候连同子孙进程一起杀


def job_tag(ai):
    return "%s#%d" % (mid_now(), state["epoch"][ai])


def mid_now():
    return os.path.basename(mdir) if mdir else None


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def save_meta():
    with lock:
        if meta is not None and mdir:
            write_json(os.path.join(mdir, "meeting.json"), meta)


def safe_title(t):
    t = re.sub(r'[\/:*?"<>|\x00-\x1f]', "", t or "")
    t = re.sub(r"\s+", "_", t.strip()).strip("._")
    return t[:40] or "会议"


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
    return m


def is_archived(status):
    return status in ("archived", "closed")   # 老数据的 closed 读作已归档


def set_current(mid):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(CUR_FILE, "w", encoding="utf-8") as f:
        f.write(mid or "")


def load_meeting(mid):
    """把某场会议读进内存，成为当前会议。失败返回 False。"""
    global meta, mdir
    d = os.path.join(MEET_DIR, mid)
    if not mid or os.path.basename(mid) != mid or not os.path.isfile(os.path.join(d, "meeting.json")):
        return False
    with open(os.path.join(d, "meeting.json"), encoding="utf-8") as f:
        m = fix_meta(json.load(f))
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
    with lock:
        meta, mdir = m, d
        messages[:] = msgs
        pending.clear()
        state["chain"] = 0
        set_current(mid)
    return True


def load_current():
    try:
        with open(CUR_FILE, encoding="utf-8") as f:
            mid = f.read().strip()
    except Exception:
        return False
    return load_meeting(mid) if mid else False


def new_meeting(title, ann="", wd=None):
    """建一场新的并切过去；旧会议原样保留（不改状态）。AI 在跑 / 排着队时不切，返回 None。"""
    global meta, mdir
    now = datetime.now()
    title = (title or "").strip() or "会议"
    with lock:
        # 忙不忙在锁里判（原来在 HTTP 那头锁外判，判完到切之间 AI 可能刚开跑，它的回复和改动记录会落进新会议）
        if busy_for():
            return None
        base ="%s_%s" % (now.strftime("%Y-%m-%d_%H%M"), safe_title(title))
        name, i = base, 1
        while os.path.exists(os.path.join(MEET_DIR, name)):
            i += 1
            name = "%s_%d" % (base, i)
        d = os.path.join(MEET_DIR, name)
        os.makedirs(os.path.join(d, "files"))
        meta, mdir = default_meta(title, ann, wd), d
        messages.clear()
        pending.clear()
        state["chain"] = 0
        save_meta()
        with open(os.path.join(d, "log.md"), "w", encoding="utf-8") as f:
            f.write("# %s\n\n创建于 %s\n\n" % (title, meta["created"]))
        open(os.path.join(d, "log.jsonl"), "a").close()
        set_current(name)
    return name


def open_meeting(mid):
    """切到某场会议。返回 (ok, 错误)。忙不忙在锁里判，理由同 new_meeting。"""
    with lock:
        if mid == mid_now():
            return True, ""
        if not os.path.isfile(os.path.join(MEET_DIR, os.path.basename(mid or ""), "meeting.json")):
            return False, "没有这场会议"
        if busy_for():
            return False, "AI 还在回复，等它们说完再切换会议"
        if not load_meeting(mid):   # 切过去不改任何一场的状态
            return False, "没有这场会议"
        return True, ""


def _scan(base, current=None):
    out = []
    if os.path.isdir(base):
        for name in os.listdir(base):
            p = os.path.join(base, name, "meeting.json")
            if not os.path.isfile(p):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    m = json.load(f)
            except Exception:
                continue
            st = m.get("status", "closed")
            out.append({"id": name, "title": m.get("title", name), "created": m.get("created", ""),
                        "status": st, "archived": is_archived(st), "current": name == current,
                        "workdir": m.get("workdir") or PROJECT})
    return out


def list_meetings():
    out = _scan(MEET_DIR, mid_now())
    out.sort(key=lambda x: (x["created"], x["id"]), reverse=True)
    out.sort(key=lambda x: x["archived"])   # 稳定排序：归档的沉到底部
    return out


def set_archived(mid, on):
    """归档 / 取消归档：只改标记。返回 (ok, 错误)。"""
    with lock:
        if mid not in [x["id"] for x in _scan(MEET_DIR)]:
            return False, "没有这场会议"
        st = "archived" if on else "open"
        if mid == mid_now():
            meta["status"] = st
            save_meta()
        else:
            p = os.path.join(MEET_DIR, mid, "meeting.json")
            with open(p, encoding="utf-8") as f:
                m = json.load(f)
            m["status"] = st
            write_json(p, m)
    return True, ""


# ===== 删除会议：先进回收站，再彻底删除 =====
def trash_dir():
    return os.path.join(LOG_DIR, "trash")


def list_trash():
    out = _scan(trash_dir())
    out.sort(key=lambda x: (x["created"], x["id"]), reverse=True)
    return out


def _unique_name(base_dir, name):
    new, i = name, 1
    while os.path.exists(os.path.join(base_dir, new)):
        i += 1
        new = "%s_%d" % (name, i)
    return new


def _tag_ref_name(d, orig):
    """目录改名前，把存档点引用用的名字记在 meeting.json 里，彻底删除时才找得到。"""
    p = os.path.join(d, "meeting.json")
    with open(p, encoding="utf-8") as f:
        m = json.load(f)
    if not m.get("ref_name"):
        m["ref_name"] = ref_safe(orig)
        write_json(p, m)


def _move_meeting(src_base, dst_base, name):
    """name 必须是 src_base 服务端列表里的目录名；两头都要落在各自目录内。返回新目录名。"""
    if os.path.basename(name or "") != name or name not in [x["id"] for x in _scan(src_base)]:
        raise ValueError("没有这场会议")
    src = os.path.join(src_base, name)
    if not in_dir(src, src_base) or _norm(os.path.dirname(src)) != _norm(src_base):
        raise ValueError("路径不安全")
    os.makedirs(dst_base, exist_ok=True)
    new = _unique_name(dst_base, name)
    dst = os.path.join(dst_base, new)
    if not in_dir(dst, dst_base):
        raise ValueError("路径不安全")
    _tag_ref_name(src, name)
    shutil.move(src, dst)
    return new


def trash_meeting(mid):
    """移进回收站。当前会议、AI 在跑时不行。返回 (ok, 错误)。"""
    with lock:
        if mid == mid_now():
            return False, "当前会议不能删，先切到别的会议"
        if busy_for():
            return False, "AI 还在回复，等它们说完再删"
        try:
            _move_meeting(MEET_DIR, trash_dir(), mid)
        except Exception as e:
            return False, str(e)
    return True, ""


def restore_meeting(name):
    with lock:
        try:
            new = _move_meeting(trash_dir(), MEET_DIR, name)
        except Exception as e:
            return False, str(e), None
    return True, "", new


def purge_refs(ref_name, cwd=None):
    """清掉 refs/roundtable/<ref_name>/* 的存档点引用。返回删掉的个数。"""
    r = git(["for-each-ref", "--format=%(refname)", "refs/roundtable/%s/" % ref_name], check=False, cwd=cwd)
    n = 0
    if r.returncode == 0:
        for ref in r.stdout.decode("utf-8", "replace").split():
            if git(["update-ref", "-d", ref], check=False, cwd=cwd).returncode == 0:
                n += 1
    return n


def purge_all():
    """清空回收站。返回 (ok, 错误)。"""
    with lock:
        names = [x["id"] for x in _scan(trash_dir())]
    for n in names:
        ok, msg = purge_meeting(n)
        if not ok:
            return False, msg
    return True, ""


def purge_meeting(name):
    """彻底删除回收站里的一场。返回 (ok, 错误)。"""
    with lock:
        item = next((x for x in _scan(trash_dir()) if x["id"] == name), None)
        if os.path.basename(name or "") != name or not item:
            return False, "回收站里没有这场会议"
        d = os.path.join(trash_dir(), name)
        if not in_dir(d, trash_dir()) or _norm(os.path.dirname(d)) != _norm(trash_dir()):
            return False, "路径不安全"
        try:
            with open(os.path.join(d, "meeting.json"), encoding="utf-8") as f:
                m = json.load(f)
        except Exception:
            m = {}
        ref_name = m.get("ref_name") or ref_safe(name)
        wd = m.get("workdir") or PROJECT
        sids = meeting_sessions(m)
        shutil.rmtree(d)
        try:
            purge_refs(ref_name, cwd=wd if os.path.isdir(wd) else None)
        except Exception:
            pass
        # 别的会议（还在的、回收站里剩下的）也在用的会话不删
        used = set()
        for base in (MEET_DIR, trash_dir()):
            for x in _scan(base):
                try:
                    with open(os.path.join(base, x["id"], "meeting.json"), encoding="utf-8") as f:
                        for a, ss in meeting_sessions(json.load(f)).items():
                            used.update((a, v) for v in ss)
                except Exception:
                    pass
        sids = {a: [v for v in ss if (a, v) not in used] for a, ss in sids.items()}
    if any(sids.values()):
        threading.Thread(target=delete_sessions, args=(m.get("title") or name, sids), daemon=True).start()
    return True, ""


SID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z\-]{7,63}$")   # 会话 id 只能是这种字符，防止拼路径拼出别的地方


def meeting_sessions(m):
    """一场会议用过的全部会话 id：{ai: [id, ...]}（当前的 + 压缩续接换下来的旧的）。"""
    out = {}
    for a in AIS:
        ss = [m.get(a + "_session")] + list(((m.get("old_sessions") or {}).get(a)) or [])
        out[a] = [v for i, v in enumerate(ss) if isinstance(v, str) and SID_RE.match(v) and v not in ss[:i]]
    return out


CLAUDE_HOME = os.path.join(os.path.expanduser("~"), ".claude")   # 测试时可替换


def delete_claude_session(sid):
    """删 claude 一个会话在本机的全部记录：projects/*/<id>.jsonl 和同名目录、file-history、session-env、todos。返回删掉的条数。"""
    if not SID_RE.match(sid or ""):
        return 0
    targets = glob.glob(os.path.join(CLAUDE_HOME, "projects", "*", sid + ".jsonl"))
    targets += [x for x in glob.glob(os.path.join(CLAUDE_HOME, "projects", "*", sid)) if os.path.isdir(x)]
    targets += [os.path.join(CLAUDE_HOME, "file-history", sid), os.path.join(CLAUDE_HOME, "session-env", sid)]
    targets += glob.glob(os.path.join(CLAUDE_HOME, "todos", sid + "*.json"))
    n = 0
    for t in targets:
        if not in_dir(t, CLAUDE_HOME) or not os.path.lexists(t):
            continue
        try:
            if os.path.isdir(t) and not os.path.islink(t):
                shutil.rmtree(t)
            else:
                os.remove(t)
            n += 1
        except OSError:
            pass
    return n


def delete_codex_thread(sid):
    """经 App Server 删 codex 的会话（连它的记录文件）。返回 (ok, 说明)。"""
    if not SID_RE.match(sid or ""):
        return False, "id 不对"
    with codex_server.call_lock:   # 正在跑的那一轮跑完才轮到它
        try:
            if not codex_server.alive():
                codex_server.stop()
                codex_server.start()
            codex_server.request("thread/delete", {"threadId": sid}, timeout=60)
            return True, ""
        except Exception as e:
            msg = str(e)
            if "not found" in msg.lower() or "no thread" in msg.lower() or "找不到" in msg:
                return True, "本来就没有"
            return False, msg


def delete_sessions(title, sids):
    """彻底删除会议后在后台删两个 AI 的会话，结果记一条系统消息（只给人看）。"""
    parts = []
    for sid in sids.get("claude", []):
        n = delete_claude_session(sid)
        parts.append("claude 会话 %s…：%s" % (sid[:8], "删了 %d 项" % n if n else "本机没找到"))
    for sid in sids.get("codex", []):
        ok, why = delete_codex_thread(sid)
        parts.append("codex 会话 %s…：%s" % (sid[:8], ("删了" + ("（%s）" % why if why else "")) if ok else "没删成（%s）" % why))
    if meta is not None:
        system_note("彻底删除「%s」：%s" % (title, "；".join(parts)))


def add_message(speaker, text, attachments=None, to_user=False, quiet=False, step=None, not_for=None, origin=None, reply_to=None):
    now = datetime.now()
    with lock:
        m = {"id": len(messages), "speaker": speaker, "text": text, "time": now.strftime("%H:%M:%S")}
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
        messages.append(m)
        if mdir:
            with open(os.path.join(mdir, "log.md"), "a", encoding="utf-8") as f:
                f.write("### %s · %s%s\n%s\n" % (speaker, m["time"], "（→ 用户）" if to_user else "", text))
                for at in attachments or []:
                    f.write("附件：%s\n" % at["path"])
                if reply_to:
                    f.write("（回复 %s 的：%s）\n" % (reply_to["speaker"], reply_to["text"]))
                f.write("\n")
            with open(os.path.join(mdir, "log.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
    return m


AIS = ("claude", "codex")
MENTION = re.compile(r"@(claude|codex|用户|user)(?![A-Za-z0-9_])", re.I)


def mentions(text, exclude=None):
    """按出现顺序返回名字列表：claude / codex / 用户。"""
    seen = []
    for mt in MENTION.finditer(text):
        n = mt.group(1).lower()
        if n == "user":
            n = "用户"
        if n != exclude and n not in seen:
            seen.append(n)
    return seen


IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif"}
AUD_EXT = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".oga", ".opus", ".caf", ".flac", ".weba"}


def kind_of(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in IMG_EXT:
        return "image"
    if ext in AUD_EXT:
        return "audio"
    return "file"


def _norm(p):
    return os.path.normcase(os.path.realpath(p))


def in_dir(full, base):
    full, base = _norm(full), _norm(base)
    try:
        return os.path.commonpath([full, base]) == base
    except ValueError:
        return False


def sanitize_name(name):
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if ord(c) >= 32 and c != "\x7f")
    name = re.sub(r"[^\w.\-() ]", "_", name, flags=re.UNICODE)
    name = name.replace("..", "_").strip(" .")
    root, ext = os.path.splitext(name or "file")
    return (root[:80] or "file") + ext[:12]


def files_dir():
    return os.path.join(mdir, "files") if mdir else None


def save_upload(name, reader, size):
    """从 reader 读 size 字节，写进当前会议的 files/ 下，返回完整路径。"""
    fd_ = files_dir()
    if not fd_:
        raise ValueError("no meeting")
    os.makedirs(fd_, exist_ok=True)
    now = datetime.now()
    root, ext = os.path.splitext("%s_%s" % (now.strftime("%H%M%S"), sanitize_name(name)))
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


def make_attachment(full):
    full = os.path.normpath(full)
    return {"name": os.path.basename(full).split("_", 1)[-1],
            "path": os.path.relpath(full, ATT_BASE).replace("\\", "/"),
            "url": "/files/" + quote(os.path.relpath(os.path.realpath(full), os.path.realpath(MEET_DIR)).replace("\\", "/")),
            "kind": kind_of(full), "size": os.path.getsize(full)}


def clean_attachments(raw):
    """校验客户端传来的附件：只认当前会议 files 目录里真实存在的文件，其余信息由服务端重算。"""
    out = []
    fd_ = files_dir()
    if not isinstance(raw, list) or not fd_:
        return out
    for a in raw[:MAX_ATTACH]:
        if not isinstance(a, dict):
            continue
        full = os.path.normpath(os.path.join(ATT_BASE, str(a.get("path", ""))))
        if in_dir(full, fd_) and os.path.isfile(full):
            out.append(make_attachment(full))
    return out


def abs_path(att):
    return os.path.normpath(os.path.join(ATT_BASE, att["path"]))


def read_announcement():
    return meta["announcement"] if meta else ""


def write_announcement(text):
    with lock:
        if meta is None:
            return
        meta["announcement"] = text
        meta["ann_ver"] += 1
        save_meta()
    add_message("系统", "公告已更新", quiet=True)


# ---------- 看板（公告 + 待办），用户和两个 AI 都能写 ----------
MAX_BOARD = 100
MAX_BOARD_TEXT = 500
BOARD_CMD = re.compile(r"^\s*[【\[](公告|待办|完成|删)[】\]]\s*(.*?)\s*$")
BOARD_KIND = {"公告": "note", "待办": "todo"}
KIND_CN = {"note": "公告", "todo": "待办"}


def board_text():
    items = (meta or {}).get("board") or []
    if not items:
        return "（看板是空的）"
    out = []
    for it in items:
        tag = "已完成" if it.get("done") else KIND_CN.get(it["kind"], "公告")
        out.append("#%d [%s] %s（%s）" % (it["id"], tag, it["text"], it["by"]))
    return "\n".join(out)


def board_op(by, op, kind=None, text="", ids=()):
    """改看板，返回给人看的一句话（没改成返回 None）。调用方负责发消息。"""
    with lock:
        if meta is None:
            return None
        board = meta["board"]
        if op == "add":
            text = (text or "").strip()[:MAX_BOARD_TEXT]
            if not text or kind not in KIND_CN or len(board) >= MAX_BOARD:
                return None
            meta["board_seq"] += 1
            board.append({"id": meta["board_seq"], "kind": kind, "text": text, "by": by,
                          "done": False, "time": now_str()})
            save_meta()
            return "加了%s #%d：%s" % (KIND_CN[kind], meta["board_seq"], text)
        hit = [it for it in board if it["id"] in ids]
        if not hit:
            return None
        if op in ("done", "undone"):
            for it in hit:
                it["done"] = op == "done"
            word = "勾掉了" if op == "done" else "取消勾选了"
        elif op == "del":
            meta["board"] = [it for it in board if it["id"] not in ids]
            word = "删了"
        else:
            return None
        save_meta()
        return word + "、".join("#%d" % it["id"] for it in hit)


def apply_board_cmds(speaker, text):
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
            r = board_op(speaker, "add", BOARD_KIND[word], body)
        else:
            ids = [int(x) for x in re.findall(r"\d+", body)]
            r = board_op(speaker, "done" if word == "完成" else "del", ids=ids)
        changes.append(r or "（没改成：%s）" % line.strip())
    return "\n".join(keep).strip(), changes


def board_note(by, changes, not_for=None):
    if changes:
        add_message("系统", "看板：%s %s" % (by, "；".join(changes)), not_for=not_for)


# ---------- 上下文用量 ----------
last_ctx = {}   # call_ai 填，process_job 成功后搬进 meta["ctx"]


def note_ctx(ai, res):
    used, win = res.get("ctx_used"), res.get("ctx_win")
    if used:
        last_ctx[ai] = {"used": int(used), "window": int(win) if win else None, "time": now_str()}


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


def delta_for(ai):
    """自 last_seen 之后、别人说的新消息（不含它自己、不含 quiet 的系统记号）。返回 (消息列表, 截止 id)。"""
    with lock:
        ls = meta["last_seen"][ai]
        upto = len(messages) - 1
        d = [m for m in messages[ls + 1:] if (m["speaker"] != ai or m.get("origin") == "external") and not m.get("quiet") and m.get("not_for") != ai]
    return d, upto


def delta_images(msgs):
    imgs = []
    for m in reversed(msgs):
        for a in reversed(m.get("attachments") or []):
            if a["kind"] == "image":
                p = abs_path(a)
                if os.path.isfile(p) and p not in imgs:
                    imgs.append(p)
    return list(reversed(imgs[:MAX_IMAGES]))


CLOSING = "现在轮到你（%s）回复。只输出你要说的话，不要加发言人前缀。"


def ann_text():
    return (meta["announcement"].strip() if meta else "") or "（暂无公告）"


def build_prompt(ai):
    """返回 (prompt, upto, ann_ver, images)。会话还没建 = 第一次叫，带前言和公告；否则只递增量。"""
    with lock:
        first = not meta.get(ai + "_session")
        d, upto = delta_for(ai)
        ver = meta["ann_ver"]
        parts = []
        mode = meta.get("mode", "discuss")
        if first:
            parts += [preamble(mode), "当前任务公告：\n" + ann_text(), "看板：\n" + board_text()]
        else:
            seen = meta["mode_seen"].get(ai) or ""
            if seen != mode_key(mode):
                head = "模式变了。" if seen.split(":")[0] != mode else "会议室规矩改了，以这一版为准（之前说的作废）："
                parts.append(head + mode_text(mode))
            if meta["announcement_seen"][ai] != ver:
                parts.append("公告已更新为：\n" + ann_text())
            if meta["returned"][ai]:
                parts.append("（你刚才在终端里跟用户单独干了一段活，那段你自己记得；下面是这期间会议室里的新消息）")
        body = "\n\n".join(fmt_message(m) for m in d) or "（没有新消息）"
        parts += [("会议记录：\n" if first else "新消息：\n") + body, CLOSING % ai]
    return "\n\n".join(parts), upto, ver, delta_images(d)


def _run(cmd, prompt):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=workdir(), env=env, timeout=TIMEOUT, **kw)


def kill_tree(p):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        else:
            p.kill()
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def run_stream(cmd, prompt, on_line=None, timeout=TIMEOUT, tag=None):
    """边跑边逐行读 stdout（让 on_line 能在 AI 中途就拿到它的操作记录；超时/崩了也不丢）。
    返回 (退出码, stdout 全文, stderr 全文, 是否超时)。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=workdir(), env=env, **kw)
    if tag:
        live_procs[tag] = p
    lines, errs = [], []

    def rd_out():
        for raw in p.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            lines.append(line)
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass

    def rd_err():
        errs.append(p.stderr.read())

    ts = [threading.Thread(target=rd_out, daemon=True), threading.Thread(target=rd_err, daemon=True)]
    for t in ts:
        t.start()
    try:
        p.stdin.write(prompt.encode("utf-8"))
        p.stdin.close()
    except OSError:
        pass
    timed_out = False
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(p)
        try:
            p.wait(timeout=15)
        except Exception:
            pass
    for t in ts:
        t.join(10)
    return p.returncode, "\n".join(lines), b"".join(errs).decode("utf-8", "replace"), timed_out


CLAUDE_READ = "Read,Grep,Glob"
CLAUDE_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
# 本机 codex 的 MCP 服务器（其中 node_repl 能读写文件），两种模式都要逐个关；实测 -c mcp_servers={} 关不掉
def codex_mcp_servers():
    """~/.codex/config.toml 里配的全部 MCP 服务名（会议室里一律关掉，回得快）。名字只认字母数字下划线横杠，别的拼不进 -c。"""
    names = list(((_codex_cfg().get("mcp_servers")) or {}).keys())
    return [n for n in names if re.match(r"^[\w\-]+$", n)]
CODEX_DISCUSS_OFF = ("apps", "computer_use", "browser_use", "image_generation")


# ===== 每场会议各自的模型 / effort =====
HOME = os.path.expanduser("~")
CLAUDE_SETTINGS = os.path.join(HOME, ".claude", "settings.json")   # 测试时可替换
CODEX_CONFIG = os.path.join(HOME, ".codex", "config.toml")
CLAUDE_MODEL_PRESETS = ["opus", "sonnet", "haiku", "fable"]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]   # claude --help 里 --effort 支持的值
CODEX_EFFORTS_DEFAULT = ["low", "medium", "high"]
MODEL_RE = re.compile(r"^[A-Za-z0-9][\w.\-:/\[\]]{0,79}$")
EFFORT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,20}$")


def global_claude(for_model=None):
    """只读 settings.json 的 model；effort 查 modelSettings.<模型>.effortLevel（for_model 缺省 = 全局 model）。读不到的是 ''。"""
    model = effort = ""
    try:
        with open(CLAUDE_SETTINGS, encoding="utf-8") as f:
            s = json.load(f)
        model = s.get("model") if isinstance(s.get("model"), str) else ""
        key = for_model or model
        ms = s.get("modelSettings")
        if key and isinstance(ms, dict):
            hit = ms.get(key)
            if not isinstance(hit, dict):   # 别名（opus）对不上全名（claude-opus-4-8）时，按"全名里含别名"找
                hit = next((v for k, v in ms.items() if isinstance(v, dict) and key.lower() in k.lower()), None)
            if isinstance(hit, dict) and isinstance(hit.get("effortLevel"), str):
                effort = hit["effortLevel"]
    except Exception:
        pass
    return model, effort


def _codex_cfg():
    try:
        import tomllib
        with open(CODEX_CONFIG, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def global_codex():
    c = _codex_cfg()
    model = c.get("model") if isinstance(c.get("model"), str) else ""
    effort = c.get("model_reasoning_effort") if isinstance(c.get("model_reasoning_effort"), str) else ""
    return model, effort


def _codex_catalog():
    """codex 自己缓存的模型清单（~/.codex/models_cache.json），只取 visibility=list 的。[(slug, [effort...])]"""
    try:
        with open(os.path.join(os.path.dirname(CODEX_CONFIG), "models_cache.json"), encoding="utf-8") as f:
            c = json.load(f)
        ms = c.get("models", []) if isinstance(c, dict) else c
        out = []
        for m in ms:
            if isinstance(m, dict) and m.get("slug") and m.get("visibility", "list") == "list":
                lv = [x.get("effort") if isinstance(x, dict) else x for x in (m.get("supported_reasoning_levels") or [])]
                out.append((m["slug"], [x for x in lv if isinstance(x, str)]))
        return out
    except Exception:
        return []


def effort_options(ai):
    if ai == "claude":
        return list(CLAUDE_EFFORTS)
    cur = effective_models("codex")[0]
    for slug, lv in _codex_catalog():
        if slug == cur and lv:
            return lv
    c = _codex_cfg()
    v = c.get("enabled-reasoning-efforts")
    if not isinstance(v, list):
        v = (c.get("desktop") or {}).get("enabled-reasoning-efforts") if isinstance(c.get("desktop"), dict) else None
    v = [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    return v or list(CODEX_EFFORTS_DEFAULT)


def model_options(ai):
    if ai == "claude":
        return list(CLAUDE_MODEL_PRESETS)
    out = [s for s, _ in _codex_catalog()]
    g = global_codex()[0]
    if g and g not in out:
        out.insert(0, g)
    return out


def own_models(ai):
    mo = ((meta or {}).get("models") or {}).get(ai) or {}
    return (mo.get("model") or "").strip(), (mo.get("effort") or "").strip()


def effective_models(ai):
    """实际生效的 (模型, effort)：会议自己设了用自己的，没设跟随全局；读不到是 ''。"""
    m, e = own_models(ai)
    gm, ge = global_claude() if ai == "claude" else global_codex()
    if not e:
        # claude 的全局 effort 是按模型存的：会议自己换了模型就查那个模型的
        e = global_claude(m)[1] if (ai == "claude" and m) else ge
    return (m or gm), e


def model_label(ai):
    m, e = effective_models(ai)
    return "%s · %s" % (m or "默认", e or "默认")


def model_info(ai):
    m, e = own_models(ai)
    return {"model": m, "effort": e, "label": model_label(ai),
            "models": model_options(ai), "efforts": effort_options(ai)}


def set_models(ai, model, effort):
    """改某个 AI 在当前会议的模型 / effort（空 = 跟随全局）。返回 (ok, 错误, 是否换了模型)。"""
    if ai not in AIS:
        return False, "未知 AI", False
    model, effort = (model or "").strip(), (effort or "").strip()
    if model and not MODEL_RE.match(model):
        return False, "模型名不合法", False
    if effort and (not EFFORT_RE.match(effort) or effort not in effort_options(ai)):
        return False, "effort 不在可选范围内", False
    with lock:
        if meta is None:
            return False, "还没有会议", False
        if busy_for():
            return False, "AI 还在回复，等它们说完再改", False
        old_m, old_e = own_models(ai)
        if (old_m, old_e) == (model, effort):
            return True, "", False
        meta["models"][ai] = {"model": model, "effort": effort}
        save_meta()
        add_message("系统", "%s 换成了 %s" % (ai, model_label(ai)), quiet=True)
    return True, "", old_m != model


def build_cmd(ai, mode, sid, exe, images=(), outf=None, model="", effort=""):
    """拼出某个 AI 在某个模式下的命令行（新建会话 / resume 在这里分）。claude 新建会话时由调用方再补 --session-id。
    model / effort 是这场会议自己设的值，空 = 不加参数（跟随全局）。"""
    if ai == "claude":
        cmd = [exe, "-p"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if mode == "work":
            # 干活模式完全放开（用户 2026-10-13 定），跟平时用 Claude Code 一样
            cmd += ["--permission-mode", "bypassPermissions"]
        else:
            cmd += ["--allowedTools", CLAUDE_READ]
        # 两种模式都不加载 MCP、不跑钩子：MCP 拖慢每一轮；会议室每轮都是新进程 --resume，
        # 开场钩子每轮都会把一大段说明重新塞进会话
        cmd += ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--settings", '{"disableAllHooks":true}']
        if not in_dir(LOG_DIR, workdir()):
            cmd += ["--add-dir", LOG_DIR]   # 工作文件夹不是本项目时，附件在会议室目录里，得让它能读
        cmd += ["--output-format", "stream-json", "--verbose"]   # 逐行事件：拿干净的最终回复，也拿它自己的写文件操作
        if sid:
            cmd += ["--resume", sid]
        return cmd
    sandbox = "workspace-write" if mode == "work" else "read-only"
    if sid:
        cmd = [exe, "exec", "resume", "--skip-git-repo-check", "-c", "sandbox_mode=" + sandbox, "--json"]
    else:
        cmd = [exe, "exec", "--sandbox", sandbox, "--skip-git-repo-check", "--color", "never", "--json"]
    if model:
        cmd += ["-m", model]   # exec 和 exec resume 都支持 -m
    if effort:
        cmd += ["-c", 'model_reasoning_effort="%s"' % effort]
    if mode == "work":
        cmd += ["-c", "sandbox_workspace_write.network_access=false"]
    # 两种模式都关 MCP：干活模式只留 codex 自带的 apply_patch，所有写入都进 --json 记录
    for srv in codex_mcp_servers():
        cmd += ["-c", "mcp_servers.%s.enabled=false" % srv]   # 键名不能加引号（加了会被当成另一张表，codex 报 invalid transport）
    for ft in CODEX_DISCUSS_OFF:
        cmd += ["--disable", ft]
    for p in images:
        cmd += ["-i", p]
    cmd += ["-o", outf] + ([sid] if sid else []) + ["-"]
    return cmd


def parse_claude_line(line, edits, res):
    """claude stream-json 一行：收 Edit/Write/MultiEdit/NotebookEdit 的路径、最终 result。"""
    try:
        j = json.loads(line)
    except Exception:
        return
    if not isinstance(j, dict):
        return
    t = j.get("type")
    if t == "assistant":
        u = (j.get("message") or {}).get("usage")
        if isinstance(u, dict) and not j.get("parent_tool_use_id"):   # 最后一次调用的输入 + 输出 ≈ 当前上下文；子助手的不算
            n = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                  "cache_creation_input_tokens", "output_tokens"))
            if n:
                res["ctx_used"] = n
        for c in (j.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in CLAUDE_EDIT_TOOLS:
                inp = c.get("input") or {}
                p = inp.get("file_path") or inp.get("notebook_path")
                if p:
                    edits.append({"path": str(p), "op": c["name"]})
    elif t == "result":
        res["result"] = j
        mu = j.get("modelUsage")
        if isinstance(mu, dict):   # 主模型 = 读得最多的那个
            best = max((v for v in mu.values() if isinstance(v, dict)),
                       key=lambda v: int(v.get("cacheReadInputTokens") or 0) + int(v.get("inputTokens") or 0), default=None)
            if best and best.get("contextWindow"):
                res["ctx_win"] = best["contextWindow"]


def parse_codex_line(line, edits, res):
    """codex --json 一行：thread id、file_change 条目里的路径和类型、跑过的命令数。"""
    if not line.startswith("{"):
        return
    try:
        j = json.loads(line)
    except Exception:
        return
    if not isinstance(j, dict):
        return
    t = j.get("type")
    if t == "thread.started":
        res["sid"] = j.get("thread_id") or res.get("sid")
    elif t in ("item.started", "item.completed"):
        it = j.get("item") or {}
        if it.get("type") == "file_change":
            for ch in it.get("changes") or []:
                if isinstance(ch, dict) and ch.get("path"):
                    edits.append({"path": str(ch["path"]), "op": str(ch.get("kind") or "update")})
        elif it.get("type") == "command_execution" and t == "item.completed" and it.get("exit_code") == 0:
            edits.append({"path": None, "op": "command"})


# ===== codex 走 App Server（常驻一个 codex app-server 子进程，stdio 上收发 JSON-RPC）=====
# 旧的每轮起一个 codex exec 只能手动切回：环境变量 ROUNDTABLE_CODEX=exec
CODEX_BACKEND = os.environ.get("ROUNDTABLE_CODEX", "appserver")
# 实测（2026-10-13，codex-cli 0.162.1）：
# - elevated 沙箱每次跑命令前都要给桌面版运行时里的 node_repl.exe 刷权限，桌面版开着就被占住（os error 32），
#   跟 config 无关（换一份干净的 CODEX_HOME 也一样），所以会议室改用 unelevated。
# - unelevated 起不了商店版 pwsh（CreateProcessAsUserW 拒绝），所以子进程的 PATH 去掉 WindowsApps，
#   让 codex 退回系统自带的 Windows PowerShell。只读挡写、可写只限项目目录，都实测过。
CODEX_SANDBOX_OVERRIDE = 'windows.sandbox="unelevated"'


def _codex_env():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                  if p and "windowsapps" not in p.lower())
    return env


def codex_policy(mode):
    if mode == "work":
        return {"type": "dangerFullAccess"}   # 干活模式完全放开（用户 2026-10-13 定），跟平时用桌面版一样
    return {"type": "readOnly", "networkAccess": False}


class CodexServer:
    """一个常驻的 codex app-server。请求串行（self.call_lock），一次只跑一轮。
    每轮：thread/resume（或新建）→ turn/start → 等 turn/completed → thread/unsubscribe。
    每轮都重新接、说完就放手：用户在终端里 codex resume 聊过的，下一轮也接得上；交给终端时会议室不占着会话。"""

    def __init__(self):
        self.p = None
        self.q = None
        self.err = []            # stderr 最后几十行
        self.next_id = 0
        self.call_lock = threading.Lock()

    # --- 进程 ---
    def alive(self):
        return self.p is not None and self.p.poll() is None

    def start(self):
        exe = shutil.which("codex")
        if not exe:
            raise RuntimeError("找不到 codex 命令")
        cmd = [exe, "-c", CODEX_SANDBOX_OVERRIDE, "-c", "notify=[]"]
        for srv in codex_mcp_servers():
            cmd += ["-c", "mcp_servers.%s.enabled=false" % srv]
        for ft in CODEX_DISCUSS_OFF:
            cmd += ["--disable", ft]
        cmd += ["app-server", "--listen", "stdio://"]
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  cwd=PROJECT, env=_codex_env(), **kw)
        self.q = queue.Queue()
        self.err = []
        p, q = self.p, self.q

        def rd_out():
            for raw in p.stdout:
                try:
                    q.put(json.loads(raw.decode("utf-8", "replace")))
                except Exception:
                    pass
            q.put(None)

        def rd_err():
            for raw in p.stderr:
                self.err.append(raw.decode("utf-8", "replace").rstrip())
                del self.err[:-40]

        threading.Thread(target=rd_out, daemon=True).start()
        threading.Thread(target=rd_err, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "roundtable", "title": "AI Bridge", "version": "1"},
                                    "capabilities": None}, timeout=120)
        self.notify("initialized", None)

    def stop(self):
        if self.p is not None:
            try:
                self.p.stdin.close()
            except Exception:
                pass
            if self.p.poll() is None:
                kill_tree(self.p)
        self.p = None

    def err_tail(self, n=6):
        return "\n".join(l for l in self.err if l.strip())[-1500:].splitlines()[-n:]

    # --- 收发 ---
    def _send(self, obj):
        self.p.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.p.stdin.flush()

    def notify(self, method, params):
        self._send({"method": method, "params": params} if params is not None else {"method": method})

    def _answer_server_request(self, m):
        """服务端反过来问的（审批、要用户输入等）：会议室一律不批，免得一轮卡住。"""
        meth, rid = m.get("method"), m.get("id")
        if meth in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            self._send({"id": rid, "result": {"decision": "decline"}})
        elif meth in ("applyPatchApproval", "execCommandApproval"):
            self._send({"id": rid, "result": {"decision": {"denied": {"rejection": "会议室不批审批请求"}}}})
        else:
            self._send({"id": rid, "error": {"code": -32601, "message": "会议室不支持 %s" % meth}})

    def _next(self, deadline):
        """取下一条消息；服务端请求就地回掉。返回 None = 进程没了；超时抛 TimeoutError。"""
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError()
            try:
                m = self.q.get(timeout=left)
            except queue.Empty:
                raise TimeoutError()
            if m is None:
                return None
            if "method" in m and "id" in m:
                self._answer_server_request(m)
                continue
            return m

    def request(self, method, params, timeout=120, on_note=None):
        self.next_id += 1
        rid = self.next_id
        self._send({"id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while True:
            m = self._next(deadline)
            if m is None:
                raise RuntimeError("codex 后台进程退出了")
            if m.get("id") == rid:
                if "error" in m:
                    e = m["error"] or {}
                    raise RuntimeError("%s 失败：%s" % (method, e.get("message") or e))
                return m.get("result") or {}
            if on_note and "method" in m:
                on_note(m)

    # --- 一轮 ---
    def run_turn(self, sid, prompt, images, mode, model, effort, edits, res):
        """返回 (ok, 回复, 会话 id)。edits 边跑边填（崩了也留着）。"""
        with self.call_lock:
            if not self.alive():
                self.stop()
                self.start()
            policy = codex_policy(mode)
            sandbox = "danger-full-access" if mode == "work" else "read-only"
            base = {"cwd": workdir(), "approvalPolicy": "never", "sandbox": sandbox}
            if model:
                base["model"] = model
            if sid:
                try:
                    self.request("thread/resume", dict(base, threadId=sid, excludeTurns=True), timeout=180)
                except RuntimeError as e:
                    return False, "codex 接不上原会话（%s），没有另开新会话" % e, sid
            else:
                r = self.request("thread/start", base, timeout=180)
                sid = ((r.get("thread") or {}).get("id")) or ""
                if not sid:
                    return False, "codex 没拿到会话 id", None
            res["sid"] = sid
            inp = [{"type": "text", "text": prompt, "text_elements": []}]
            inp += [{"type": "localImage", "path": p} for p in images]
            tp = {"threadId": sid, "input": inp, "cwd": workdir(), "approvalPolicy": "never", "sandboxPolicy": policy}
            if model:
                tp["model"] = model
            if effort:
                tp["effort"] = effort
            msgs, final, state = [], [], {"turn": None, "done": None, "error": ""}

            def on_note(m):
                meth, pr = m.get("method"), m.get("params") or {}
                if pr.get("threadId") not in (None, sid):
                    return
                if meth in ("item/started", "item/completed"):
                    it = pr.get("item") or {}
                    t = it.get("type")
                    if t == "fileChange":   # started 也记：没写成的路径多记几条无害，漏记会撤不回
                        for ch in it.get("changes") or []:
                            if isinstance(ch, dict) and ch.get("path"):
                                kind = (ch.get("kind") or {}).get("type") if isinstance(ch.get("kind"), dict) else ch.get("kind")
                                edits.append({"path": str(ch["path"]), "op": str(kind or "update")})
                    elif t == "commandExecution" and meth == "item/completed" and it.get("exitCode") == 0:
                        edits.append({"path": None, "op": "command"})
                    elif t == "agentMessage" and meth == "item/completed" and (it.get("text") or "").strip():
                        msgs.append(it["text"].strip())
                        if it.get("phase") == "final_answer":
                            final.append(it["text"].strip())
                elif meth == "thread/tokenUsage/updated":
                    tu = pr.get("tokenUsage") or {}
                    last = tu.get("last") or {}
                    if last.get("totalTokens"):
                        res["ctx_used"] = last["totalTokens"]
                    if tu.get("modelContextWindow"):
                        res["ctx_win"] = tu["modelContextWindow"]
                elif meth == "error":
                    e = pr.get("error") or {}
                    if not pr.get("willRetry"):
                        state["error"] = e.get("message") or ""
                elif meth == "turn/completed":
                    turn = pr.get("turn") or {}
                    if state["turn"] is None or turn.get("id") == state["turn"]:
                        state["done"] = turn

            try:
                r = self.request("turn/start", tp, timeout=120, on_note=on_note)
                state["turn"] = (r.get("turn") or {}).get("id")
                deadline = time.time() + timeout_for(mode)
                while state["done"] is None:
                    try:
                        m = self._next(deadline)
                    except TimeoutError:
                        self._interrupt(sid, state)
                        return False, "codex 超时了", sid
                    if m is None:
                        return False, "codex 后台进程中途退出了\n" + "\n".join(self.err_tail()), sid
                    on_note(m)
            finally:
                if self.alive():
                    try:
                        self.request("thread/unsubscribe", {"threadId": sid}, timeout=30)
                    except Exception:
                        pass
            turn = state["done"]
            status = turn.get("status")
            if status != "completed":
                msg = ((turn.get("error") or {}).get("message")) or state["error"] or status
                return False, "codex 这一轮没跑完（%s）" % msg, sid
            out = "\n\n".join(final) if final else (msgs[-1] if msgs else "")
            return True, out, sid

    def _interrupt(self, sid, state):
        try:
            if state.get("turn"):
                self.request("turn/interrupt", {"threadId": sid, "turnId": state["turn"]}, timeout=30)
            deadline = time.time() + 30
            while state["done"] is None:
                m = self._next(deadline)
                if m is None:
                    break
                if m.get("method") == "turn/completed":
                    state["done"] = (m.get("params") or {}).get("turn") or {}
        except Exception:
            self.stop()   # 停不下来就整个重起，下一轮自动起新的


codex_server = CodexServer()
atexit.register(codex_server.stop)   # 被强杀时跑不到；那时 app-server 的 stdin 断了也会自己退


def call_codex_appserver(prompt, sid, images, mode, model, effort, edits, res):
    try:
        ok, out, new_sid = codex_server.run_turn(sid, prompt, list(images), mode, model, effort, edits, res)
    except Exception as e:
        tail = "\n".join(codex_server.err_tail())
        codex_server.stop()
        return False, "codex 调用失败：%s%s" % (e, ("\n" + tail) if tail else ""), sid
    if not ok:
        return False, out, sid
    if not out:
        return False, "codex 没有返回内容", sid
    return True, out, new_sid


def call_ai(ai, prompt, session_id=None, images=(), mode=None):
    """返回 (ok, text, session_id, edits)。session_id 为空 = 新建会话，返回新会话 id。mode 缺省取当前会议的模式。
    edits = 这个 AI 自己的写文件操作记录 [{path, op}]（干活模式靠它认定"这一步改了什么"）；失败/超时也尽量带上。"""
    edits, res = [], {}
    exe = shutil.which(ai)
    if not exe:
        return False, "找不到 %s 命令" % ai, session_id, edits
    if mode is None:
        mode = (meta or {}).get("mode", "discuss")
    sid = session_id
    mo_m, mo_e = own_models(ai)
    try:
        if ai == "claude":
            cmd = build_cmd(ai, mode, sid, exe, model=mo_m, effort=mo_e)
            if not sid:
                sid = str(uuid.uuid4())
                cmd += ["--session-id", sid]
            rc, so, se, to = run_stream(cmd, prompt, lambda l: parse_claude_line(l, edits, res), timeout_for(mode), ai)
            r = res.get("result") or {}
            out = (r.get("result") or "").strip() if isinstance(r.get("result"), str) else ""
            bad = bool(r.get("is_error"))
            tail_src = (out if bad else "") or se or ""
        elif CODEX_BACKEND != "exec":
            # 会话里的模型 / effort 设一次会一直留着，所以"跟随全局"时也把全局值明着传，免得沿用上一次的
            em, ee = effective_models("codex")
            r = call_codex_appserver(prompt, sid, images, mode, mo_m or em, mo_e or ee, edits, res)
            if r[0]:
                note_ctx(ai, res)
            return r + (edits,)
        else:
            fd, outf = tempfile.mkstemp(suffix=".txt", prefix="rt_codex_")
            os.close(fd)
            try:
                cmd = build_cmd(ai, mode, sid, exe, images, outf, mo_m, mo_e)
                rc, so, se, to = run_stream(cmd, prompt, lambda l: parse_codex_line(l, edits, res), timeout_for(mode), ai)
                sid = res.get("sid") or sid
                try:
                    with open(outf, encoding="utf-8", errors="replace") as f:
                        out = f.read().strip()
                except Exception:
                    out = ""
            finally:
                try:
                    os.remove(outf)
                except Exception:
                    pass
            bad = False
            tail_src = se or so or ""
        if to:
            return False, "%s 超时了" % ai, session_id, edits
        if rc != 0 or bad:
            tail = "\n".join(tail_src.strip().splitlines()[-6:])
            return False, "%s 出错了（退出码 %s）\n%s" % (ai, rc, tail), session_id, edits
        if not out:
            return False, "%s 没有返回内容" % ai, session_id, edits
        if not sid:
            return False, "%s 没拿到会话 id" % ai, session_id, edits
        note_ctx(ai, res)
        return True, out, sid, edits
    except Exception as e:
        return False, "%s 调用失败：%s" % (ai, e), session_id, edits


def claude_boundaries(sid):
    """会话文件里 compact_boundary 的个数；找不到文件返回 None。"""
    for p in glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects", "*", sid + ".jsonl")):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read().count('"compact_boundary"')
        except Exception:
            return None
    return None


def compact_native(ai, sid):
    """能真压缩就压缩，返回 True；这个 AI 不支持（codex exec 把 /compact 当普通文字）返回 False。"""
    if ai != "claude":
        return False
    exe = shutil.which("claude")
    if not exe:
        return False
    before = claude_boundaries(sid)
    try:
        r = _run([exe, "-p", "--resume", sid, "--allowedTools", "Read,Grep,Glob", "--output-format", "text"], "/compact")
    except Exception:
        return False
    if r.returncode != 0:
        return False
    after = claude_boundaries(sid)
    if before is None or after is None:
        return True
    return after > before


# ===== 干活模式：存档点、改动清单、撤回 =====
snap_lock = threading.Lock()
GIT_C = ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.quotepath=false",
         "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false"]
GIT_IDENT = {"GIT_AUTHOR_NAME": "roundtable", "GIT_AUTHOR_EMAIL": "roundtable@localhost",
             "GIT_COMMITTER_NAME": "roundtable", "GIT_COMMITTER_EMAIL": "roundtable@localhost"}
ZERO_SHA = "0" * 40
STATUS_CN = {"M": "改", "A": "增", "D": "删", "T": "改"}
MAX_LIST = 40


def git(args, env_extra=None, check=True, cwd=None):
    env = dict(os.environ, **GIT_IDENT)
    if env_extra:
        env.update(env_extra)
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    r = subprocess.run(["git"] + GIT_C + list(args), cwd=cwd or workdir(), env=env, capture_output=True, timeout=600, **kw)
    if check and r.returncode != 0:
        raise RuntimeError("git %s 失败：%s" % (args[0], r.stderr.decode("utf-8", "replace").strip()[:300]))
    return r.stdout if check else r


def git_dirs():
    out = git(["rev-parse", "--show-toplevel", "--absolute-git-dir"]).decode("utf-8", "replace").splitlines()
    return os.path.normpath(out[0]), os.path.normpath(out[1])


def snapshot_index(top):
    """私有 index 每个仓库一份（换了工作文件夹不能混用）。"""
    key = os.path.normcase(os.path.realpath(top))
    return os.path.join(LOG_DIR, "snapshot-%s.index" % hashlib.sha1(key.encode("utf-8")).hexdigest()[:10])


def in_git():
    try:
        return git(["rev-parse", "--show-toplevel"], check=False).returncode == 0
    except Exception:
        return False


def snapshot_tree():
    """不碰用户的 index 和当前分支，把工作区现状（不含 .gitignore 忽略的、不含会议室自己的目录）拍成一棵 tree，返回 tree id。
    用一份私有 index 文件（首次从用户 index 复制，之后沿用，保住文件状态缓存，不用每次重算大文件）。"""
    with snap_lock:
        top, gd = git_dirs()
        idx = snapshot_index(top)
        os.makedirs(LOG_DIR, exist_ok=True)
        env = {"GIT_INDEX_FILE": idx}
        spec = [":/"]
        # 会议室目录不在这个仓库里（工作文件夹换到别的盘时 relpath 会直接抛 ValueError）：不用排除，也不用算相对路径
        inside = in_dir(LOG_DIR, top)
        rel = os.path.relpath(os.path.realpath(LOG_DIR), os.path.realpath(top)).replace("\\", "/") if inside else ""
        # 会议室目录已经被 .gitignore 忽略时不用再排除；点名排除被忽略的路径，git add 会报错退出
        ignored = inside and git(["check-ignore", "-q", rel + "/"], check=False).returncode == 0
        if inside and not ignored:
            # 只排除会议室自己天天在写的：会议记录目录、current.txt、私有 index；这个目录里别的文件照常算
            for x in ("meetings", "trash", "current.txt"):
                spec.append(":(exclude,top)%s/%s" % (rel, x))
            spec.append(":(exclude,top,glob)%s/snapshot*.index*" % rel)
        for attempt in (0, 1):
            if not os.path.exists(idx):
                real = os.path.join(gd, "index")
                if os.path.exists(real):
                    shutil.copyfile(real, idx)
                elif git(["rev-parse", "--verify", "-q", "HEAD"], check=False).returncode == 0:
                    git(["read-tree", "HEAD"], env)
            try:
                git(["add", "-A", "--"] + spec, env)
                break
            except RuntimeError:
                if attempt:
                    raise
                try:
                    os.remove(idx)
                except OSError:
                    pass
        return git(["write-tree"], env).decode().strip()


def ref_safe(name):
    n = re.sub(r"[^\w\-]", "_", name, flags=re.UNICODE)
    return n.strip("_.") or "m"


def save_checkpoint(seq, pre_tree):
    """把"改前"的树包成一个提交，挂在 refs/roundtable/<会议>/<序号> 上，防止被 gc（撤回要从这里取原文件）。"""
    head = git(["rev-parse", "--verify", "-q", "HEAD"], check=False)
    parent = head.stdout.decode().strip() if head.returncode == 0 else None
    base = "refs/roundtable/%s/%d" % ((meta or {}).get("ref_name") or ref_safe(mid_now() or "m"), seq)
    c0 = git(["commit-tree", pre_tree] + (["-p", parent] if parent else []) + ["-m", "roundtable checkpoint"]).decode().strip()
    git(["update-ref", base, c0])
    return base


def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


def tree_blobs(tree, rels):
    """某棵树里这些路径各自的 blob id（树里没有 = None）。一次 git cat-file --batch-check 搞定。"""
    if not rels:
        return {}
    inp = "".join("%s:%s\n" % (tree, r) for r in rels).encode("utf-8")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    r = subprocess.run(["git"] + GIT_C + ["cat-file", "--batch-check"], cwd=workdir(), input=inp, capture_output=True,
                       env=dict(os.environ, **GIT_IDENT), timeout=120, **kw)
    if r.returncode != 0:
        raise RuntimeError("git cat-file 失败：" + r.stderr.decode("utf-8", "replace").strip()[:300])
    lines = r.stdout.decode("utf-8", "replace").splitlines()
    out = {}
    for rel, ln in zip(rels, lines):
        out[rel] = ln.split()[0] if re.match(r"^[0-9a-f]{40} blob \d+$", ln.strip()) else None
    return out


def snap_excluded(full):
    """会议室自己的目录里不进快照的那些（meetings / current.txt / 私有 index）：改了也无存档可还原。"""
    if not in_dir(full, LOG_DIR):
        return False
    first = os.path.relpath(os.path.realpath(full), os.path.realpath(LOG_DIR)).split(os.sep)[0]
    return first in ("meetings", "trash", "current.txt", "snapshot.index", "snapshot.index.lock")


def is_ignored(full):
    return git(["check-ignore", "-q", "--", full], check=False).returncode == 0


def split_edits(edits, top):
    """把 AI 的操作记录里的路径规范成相对仓库根的正斜杠路径。返回 ([(rel, full)], [项目外的绝对路径])，各自去重保序。"""
    inside, outside, seen = [], [], set()
    for e in edits or []:
        p = e.get("path")
        if not p:
            continue
        full = os.path.normpath(p if os.path.isabs(p) else os.path.join(workdir(), p))
        key = os.path.normcase(full)
        if key in seen:
            continue
        seen.add(key)
        if in_dir(full, top):
            inside.append((os.path.relpath(full, top).replace("\\", "/"), full))
        else:
            outside.append(full)
    return inside, outside


def build_files(pre_tree, inside):
    """对 AI 记录里的每个文件：读"改前"（pre_tree 里的 blob）和"改后"（现在磁盘上的字节，存 sha256）。没变化的不算。"""
    blobs = tree_blobs(pre_tree, [r for r, _ in inside])
    files = []
    for rel, full in inside:
        pre = blobs.get(rel)
        data = None
        if os.path.isfile(full):
            with open(full, "rb") as fh:
                data = fh.read()
        elif os.path.lexists(full):
            continue   # 目录之类，不算
        if data is None and pre is None:
            continue
        if data is not None and pre is not None and blob_sha(data) == pre:
            continue   # 它碰了但内容没变
        nb = False
        if pre is None:   # 改前快照里没有它：新建的，或者是被 git 忽略 / 会议室自己目录里的 -> 还原不了
            nb = snap_excluded(full) or is_ignored(full)
        files.append({"path": rel, "st": "A" if pre is None else ("D" if data is None else "M"), "pre": pre,
                      "post": None if data is None else sha256_of(data), "nb": nb})
    return files


def files_text(ai, files, outside=(), clash=None, ncmd=0):
    lines = ["%s 改了这些文件：" % ai if files else "%s 的操作记录：" % ai]
    for f in files[:MAX_LIST]:
        lines.append("%s %s%s" % (STATUS_CN.get(f["st"], f["st"]), f["path"], "（被 git 忽略 / 不在存档范围，撤不回）" if f.get("nb") else ""))
    if len(files) > MAX_LIST:
        lines.append("…另有 %d 个" % (len(files) - MAX_LIST))
    for o in list(outside)[:MAX_LIST]:
        lines.append("⚠ 改了项目外的文件：%s（撤不回）" % o)
    for p, other in (clash or {}).items():
        lines.append("⚠ 和 %s 同时改了这个文件：%s" % (other, p))
    if ncmd:
        lines.append("⚠ 它还跑了 %d 条命令，命令写的文件不在记录里" % ncmd)
    return "\n".join(lines)


def record_step(ai, pre_tree, edits, t0, t1):
    """AI 跑完后，按它自己的操作记录生成这一步的改动清单（只有记录里出现的文件才算）。有改动就记一步、插系统消息。"""
    ncmd = sum(1 for e in edits or [] if e.get("op") == "command")
    try:
        top, _ = git_dirs()
        inside, outside = split_edits(edits, top)
        files = build_files(pre_tree, inside) if inside else []
    except Exception as e:
        add_message("系统", "改动记录生成失败：%s。%s 这一步改了什么看不到，也撤不回" % (e, ai))
        return None
    if not files and not outside and not ncmd:
        return None
    seq = None
    with lock:
        mine = {f["path"] for f in files}
        clash = {}
        for o in meta["steps"].values():   # 并行时和对方时间上重叠、又改了同一个文件
            if o["ai"] == ai or o.get("undone") or o.get("t0") is None:
                continue
            if o["t0"] < t1 and t0 < o["t1"]:
                for f in o["files"]:
                    if f["path"] in mine:
                        clash.setdefault(f["path"], o["ai"])
        if files:
            meta["step_seq"] += 1
            seq = meta["step_seq"]
            ref = None
            try:
                ref = save_checkpoint(seq, pre_tree)
            except Exception as e:
                add_message("系统", "存档点引用没存上（%s），撤回仍可用，但 git gc 后可能失效" % e)
            meta["steps"][str(seq)] = {"v": 2, "ai": ai, "pre_tree": pre_tree, "ref": ref, "files": files,
                                      "outside": outside, "undone": False, "time": now_str(), "t0": t0, "t1": t1}
            save_meta()
    add_message("系统", files_text(ai, files, outside, clash, ncmd), step=seq, not_for=ai)
    return seq


def _full_of(top, rel):
    full = os.path.normpath(os.path.join(top, rel))
    if not in_dir(full, top) or in_dir(full, LOG_DIR):
        raise ValueError("路径不安全：" + rel)
    return full


def undo_plan(seq):
    """算出撤回这一步要动哪些文件（不动磁盘）。调用方持有 lock。返回 (ok, 错误, 计划)。
    冲突检查：记录里每个文件现在的 sha256 必须等于这一步记下的"改后"；对不上整步拒绝。"""
    if meta is None:
        return False, "还没有会议", None
    st = meta["steps"].get(str(seq))
    if not st:
        return False, "没有这一步", None
    if st.get("undone"):
        return False, "这一步已经撤回过了", None
    if busy_for():
        return False, "AI 还在回复，等它们说完再撤回", None
    # 两个 AI 时间上重叠、改了同一个文件：这一步的"改前"是自己开工那一刻拍的，不含对方同时写进去的东西，
    # 撤回会把对方的改动一起抹掉。不管哪一步先记下，只要对方那步还没撤，就整步拒绝
    mine = {f["path"] for f in st["files"] if not f.get("nb")}
    shared, who = [], set()
    if st.get("t0") is not None:
        for o in meta["steps"].values():
            if o is st or o["ai"] == st["ai"] or o.get("undone") or o.get("t0") is None:
                continue
            if o["t0"] < st["t1"] and st["t0"] < o["t1"]:
                for f in o["files"]:
                    if f["path"] in mine and f["path"] not in shared:
                        shared.append(f["path"])
                        who.add(o["ai"])
    if shared:
        return False, "%s 同时也改过这些文件，撤回会把它的改动一起抹掉，不能撤：" % "、".join(sorted(who)) \
            + "、".join(shared[:8]) + ("…" if len(shared) > 8 else ""), None
    legacy = st.get("v") != 2   # 旧版（整体快照差异）的步：改后用 git blob sha1 比
    try:
        top, _ = git_dirs()
        items, conflicts = [], []
        for f in st["files"]:
            full = _full_of(top, f["path"])
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
                    ok = (blob_sha(data) if legacy else sha256_of(data)) == f["post"]
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


def undo_preview(seq):
    """给确认框用：返回 (ok, 错误, 清单)。"""
    with lock:
        ok, err, plan = undo_plan(seq)
    return ok, err, plan


def undo_step(seq):
    """撤回某一步：只动这个 AI 这一步记录里的文件。返回 (ok, 错误)。"""
    with lock:
        ok, err, plan = undo_plan(seq)
        if not ok:
            return False, err
        st = meta["steps"][str(seq)]
        ai = st["ai"]
        try:
            top, _ = git_dirs()
            todo = [f for f in st["files"] if not f.get("nb")]
            olds = {}
            for f in todo:
                if f["pre"] is not None:
                    olds[f["path"]] = git(["cat-file", "blob", f["pre"]])
            for f in todo:
                full = _full_of(top, f["path"])
                if f["pre"] is None:
                    if os.path.lexists(full):
                        os.remove(full)
                    d = os.path.dirname(full)
                    while in_dir(d, top) and _norm(d) != _norm(top):
                        try:
                            os.rmdir(d)
                        except OSError:
                            break
                        d = os.path.dirname(d)
                else:
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    tmp = full + ".rt_undo.tmp"
                    with open(tmp, "wb") as fh:
                        fh.write(olds[f["path"]])
                    os.replace(tmp, full)
        except Exception as e:
            return False, "撤回出错：%s" % e
        st["undone"] = True
        save_meta()
        nb = len(st["files"]) - len(todo)
    add_message("系统", "已撤回 %s 的这一步（%d 个文件%s）" % (ai, len(todo), "，另有 %d 个撤不回" % nb if nb else ""))
    return True, ""


def set_mode(mode):
    """切模式。返回 (ok, 错误)。"""
    if mode not in MODES:
        return False, "未知模式"
    with lock:
        if meta is None:
            return False, "还没有会议"
        if meta.get("mode") == mode:
            return True, ""
        if busy_for():
            return False, "AI 还在回复，等它们说完再切模式"
        meta["mode"] = mode
        state["chain"] = 0
        save_meta()
        add_message("系统", "切到干活模式" if mode == "work" else "切回讨论模式")
    return True, ""


# 测试时可替换
ai_caller = call_ai
ai_native_compact = compact_native


def enqueue(ai, user=False):
    """排上一个 AI 的回复。已排上还没开始的不重复排（它开始时才取增量，自然会看到新消息）。
    user=True（用户发言触发）：立刻开线程，和另一个 AI 并行；
    user=False（AI 之间的对话）：进全局串行队列，一个跑完下一个才开始。两种都要等自己 AI 的锁（同一个 AI 永远串行）。"""
    with lock:
        # 用户发话不能被串行队列挡住：它已经排在 AI 之间的队列里（前面可能是别人的长活）时，照样马上另开一个并行的。
        # 队列里那份轮到时发现该看的都看过了，会自己跳过。
        if ai in pending and not user:
            return
        pending.append(ai)
        mid = job_tag(ai)
        if user:
            threading.Thread(target=run_job, args=(ai, mid, True), daemon=True).start()
        else:
            jobs.put((mid, "reply", ai))


def system_note(text):
    add_message("系统", text, quiet=True)


QUOTE_LEN = 200


def make_reply(mid, part=None):
    """用户引用的那条：只认会议里真有的、人或 AI 说的话；摘一段原文（太长截断）。part = 用户在那条里选中的一段（右键引用），有就只引这段。"""
    try:
        mid = int(mid)
    except (TypeError, ValueError):
        return None
    with lock:
        if not 0 <= mid < len(messages):
            return None
        m = messages[mid]
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


def handle_message(speaker, text, attachments=None, origin="room", reply_to=None):
    """记录一条消息并按 @ 路由。origin：room = 用户或会议室自己叫起来的 AI；external = /api/post 来的会议室外的 claude/codex。"""
    external = origin == "external"
    with lock:
        if meta is None:
            return False
        if speaker == "你" and not attachments and text.strip().lower() == "/compact":
            request_compact()
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
        changes = []
        if speaker in AIS:
            text, changes = apply_board_cmds(speaker, text)
        if text or attachments or not changes:
            add_message(speaker, text, attachments, to_user, origin="external" if external else None, reply_to=reply_to)
        board_note(speaker, changes, not_for=None if external else speaker)
        if speaker == "你":
            state["chain"] = 0
        for t in targets:
            if meta["handed_off"][t]:
                system_note("%s 现在在终端里，回来后会补看" % t)
            else:
                enqueue(t, user=(speaker == "你"))
    return True


def request_compact():
    with lock:
        state["compact_q"] += 1
        jobs.put((mid_now(), "compact", None))
        system_note("开始压缩…")


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


def wrapup_text():
    if os.path.isfile(os.path.join(workdir(), ".claude", "skills", "wrapup", "SKILL.md")):
        return WRAPUP_TEXT
    return WRAPUP_GENERIC.replace("{wd}", workdir())


def request_wrapup():
    """叫 claude 按 wrapup 落盘并提交推送；这一轮强制干活模式。返回 (ok, 错误)。"""
    with lock:
        if meta is None:
            return False, "还没有会议"
        if meta["handed_off"]["claude"]:
            return False, "claude 现在在终端里，先收回再落盘"
        if busy_for("claude"):
            return False, "claude 还在回复或排队，等它说完再落盘"
        state["force_work"].add("claude")
        add_message("系统", wrapup_text(), not_for="codex")
        enqueue("claude", user=True)
    return True, ""


def stop_ais(targets=AIS):
    """用户按"停"：targets 里正在跑的 AI 连同它起的命令（git push 之类）一起杀掉，它们排着的全部作废；别的 AI 不受影响。
    返回给人看的描述。"""
    targets = [a for a in targets if a in AIS]
    with lock:
        if meta is None:
            return "还没有会议"
        for a in targets:
            state["epoch"][a] += 1     # 已经排上的（队列里、等锁的）开跑时对不上计数，自己放弃
        pending[:] = [a for a in pending if a not in targets]
        keep = []
        while True:
            try:
                item = jobs.get_nowait()
            except queue.Empty:
                break
            if item[1] != "reply" or item[2] not in targets:
                keep.append(item)       # 压缩、别的 AI 的不归这次"停"管
        for item in keep:
            jobs.put(item)
        running = [a for a in state["running"] if a in targets]
        state["stopped"].update(running)
        for a in targets:
            state["force_work"].discard(a)
    for a in running:
        p = live_procs.get(a)
        if p is not None and p.poll() is None:
            kill_tree(p)
        if a == "codex" and CODEX_BACKEND != "exec":
            codex_server.stop()         # 杀掉整个 app-server 进程树；下一轮自动重起，会话从记录里接上
    who = "、".join(targets)
    if running:
        msg = "你强行打断了：%s。%s排着的也清掉了。" % ("、".join(running), who)
        msg += "已经改了的文件不会自动还原，干活模式下可以按步骤撤回；起的命令（比如 git push）也一起停了，可能只做了一半。"
    else:
        msg = "你按了停（%s）：排着的都清掉了。" % who
    add_message("系统", msg)
    return msg


def stop_all():
    return stop_ais(AIS)


def busy_for(ai=None):
    with lock:
        if state["compact_q"] or "compact" in state["running"]:
            return True
        if ai is None:
            return bool(state["running"] or pending)
        return ai in state["running"] or ai in pending


def run_job(ai, mid, user):
    try:
        process_job(ai, mid, user)
    except Exception as e:
        add_message("系统", "内部错误：%s" % e)


def process_job(ai, mid, user=False):
    """叫一次 AI。整个过程持有这个 AI 自己的锁（同一个 AI 永远串行）；增量在拿到锁之后才取快照。"""
    with ai_locks[ai]:
        with lock:
            if ai in pending:
                pending.remove(ai)
            forced = ai in state["force_work"]   # 先取走：下面提前返回时也不能留到下一轮
            state["force_work"].discard(ai)
            if mid != job_tag(ai) or meta is None or meta["handed_off"][ai]:
                return
            if not user and meta.get(ai + "_session") and not delta_for(ai)[0]:
                return   # AI 之间的对话排到它时，该看的它已经看过了，不白叫
            state["running"].append(ai)
        try:
            mode = "work" if forced else meta.get("mode", "discuss")
            pre = None
            t0 = time.time()
            if mode == "work" and not in_git():
                with lock:
                    warn = not meta.get("nogit_warned")
                    meta["nogit_warned"] = True
                    save_meta()
                if warn:
                    add_message("系统", "工作文件夹 %s 不是 git 仓库：干活模式照常改，但改了什么记不下来，也撤不回" % workdir())
            elif mode == "work":
                try:
                    pre = snapshot_tree()   # 只用来取"改前"原文件；改了哪些文件以 AI 自己的操作记录为准
                except Exception as e:
                    add_message("系统", "存档点拍不出来：%s。干活模式下不叫 %s" % (e, ai))
                    return
            prompt, upto, ver, imgs = build_prompt(ai)
            sid = meta.get(ai + "_session")
            try:
                res = ai_caller(ai, prompt, sid, imgs, mode)   # 明着传：落盘那一轮是强制干活模式，跟会议的模式不一样
            except BaseException:
                if pre is not None:
                    add_message("系统", "%s 调用中途崩了，这一步改了什么没能记录" % ai)
                raise
            ok, out, new_sid = res[0], res[1], res[2]
            with lock:
                was_stopped = ai in state["stopped"]
                state["stopped"].discard(ai)
            if was_stopped and not ok:
                ok, out = False, "%s 被你打断了" % ai
            edits = res[3] if len(res) > 3 else []
            if ok:
                with lock:
                    meta[ai + "_session"] = new_sid
                    meta["last_seen"][ai] = upto
                    meta["announcement_seen"][ai] = ver
                    meta["mode_seen"][ai] = mode_key(mode)
                    meta["returned"][ai] = False
                    if ai in last_ctx:
                        meta["ctx"][ai] = last_ctx.pop(ai)
                    save_meta()
                    state["chain"] += 1
                if was_stopped:   # 刚好在"停"的时候说完：话照登，但里面的 @ 不再叫人
                    rest, ch = apply_board_cmds(ai, out)
                    if rest or not ch:
                        add_message(ai, rest, to_user="用户" in mentions(rest, exclude=ai))
                    board_note(ai, ch, not_for=ai)
                else:
                    handle_message(ai, out)
            else:
                add_message("系统", out)
            if pre is not None:
                record_step(ai, pre, edits, t0, time.time())
        finally:
            with lock:
                if ai in state["running"]:
                    state["running"].remove(ai)


def compact_one(ai):
    """压缩一个 AI 的会话。返回给人看的结果描述。"""
    with lock:
        sid = meta.get(ai + "_session")
        handed = meta["handed_off"][ai]
    if handed:
        return "%s 在终端里，没压" % ai
    if not sid:
        return "%s 还没建会话，不用压" % ai
    if ai_native_compact(ai, sid):
        with lock:
            meta["ctx"][ai] = None   # 压完多少不知道，下一轮回话后再显示
            save_meta()
        return "%s 真压缩了" % ai
    # 退化：让它写摘要，再新建会话，第一条消息是前言 + 公告 + 摘要
    d, upto = delta_for(ai)
    body = "\n\n".join(fmt_message(m) for m in d)
    ask = ("会议室要压缩上下文了。请把这场会议到目前为止的要点写成一份摘要："
           "已定的结论、没解决的问题、各人的立场、谁答应了要做什么、重要的文件路径。"
           "不要 @ 任何人，直接输出摘要。\n\n" + ("还没递给你的新消息：\n" + body if body else "（没有新消息）"))
    ok, summary = (lambda r: (r[0], r[1]))(ai_caller(ai, ask, sid, ()))
    if not ok:
        return "%s 压缩失败（写摘要出错：%s）" % (ai, summary.splitlines()[0] if summary else "")
    with lock:
        ver = meta["ann_ver"]
        mode = meta.get("mode", "discuss")
    first = "\n\n".join([preamble(mode), "当前任务公告：\n" + ann_text(), "看板：\n" + board_text(),
                         "这场会议到目前为止的摘要：\n" + summary.strip() + "\n\n以上是这场会议之前的摘要。",
                         "收到后只回复“收到”两个字。"])
    r2 = ai_caller(ai, first, None, ())
    ok, new_sid = r2[0], r2[2]
    if not ok or not new_sid:
        return "%s 压缩失败（新建会话出错）" % ai
    with lock:
        if sid and sid != new_sid:
            meta["old_sessions"][ai].append(sid)   # 摘要续接换了新会话；旧的留着记录，删会议时一起删
        meta[ai + "_session"] = new_sid
        meta["last_seen"][ai] = upto
        meta["announcement_seen"][ai] = ver
        meta["mode_seen"][ai] = mode_key(mode)
        meta["returned"][ai] = False
        meta["ctx"][ai] = last_ctx.pop(ai, None)
        save_meta()
    return "%s 续接（摘要 + 新会话）" % ai


def process_compact(mid):
    with lock:
        state["compact_q"] = max(0, state["compact_q"] - 1)
        if mid != mid_now() or meta is None:
            return
        state["running"].append("compact")
    try:
        res = []
        # codex 在 exec resume 下会自己自动压缩（2026-10-13 实测，会话记录里出现 compacted），这里只压 claude
        for ai in ("claude",):
            try:
                with ai_locks[ai]:   # 等这个 AI 手上的活干完
                    res.append(compact_one(ai))
            except Exception as e:
                res.append("%s 压缩出错：%s" % (ai, e))
        system_note("已压缩：" + "；".join(res))
    finally:
        with lock:
            if "compact" in state["running"]:
                state["running"].remove("compact")


def set_handoff(ai, on):
    """返回 (ok, 错误或提示)。"""
    if ai not in AIS:
        return False, "未知 AI"
    with lock:
        if meta is None:
            return False, "还没有会议"
        if on == meta["handed_off"][ai]:
            return True, ""
        if on:
            if not meta.get(ai + "_session"):
                return False, "还没叫过它，先在会议里 @ 它一次"
            if busy_for(ai):
                return False, "它正在跑或排着队，等它说完再交"
            meta["handed_off"][ai] = True
            save_meta()
            system_note("%s 去终端了" % ai)
        else:
            meta["handed_off"][ai] = False
            meta["returned"][ai] = True
            save_meta()
            system_note("%s 回到会议室" % ai)
    return True, ""


def handoff_cmd(ai):
    sid = meta.get(ai + "_session") if meta else None
    if not sid:
        return None
    return "cd \"%s\"; %s" % (workdir(), "claude --resume " + sid if ai == "claude" else "codex resume " + sid)


def worker():
    while True:
        mid, kind, ai = jobs.get()
        try:
            if kind == "compact":
                process_compact(mid)
            else:
                process_job(ai, mid, False)
        except Exception as e:
            add_message("系统", "内部错误：%s" % e)


def api_state():
    with lock:
        if meta is None:
            return {"meeting": None, "running": list(state["running"]), "queued": len(pending)}
        ais = {}
        for a in AIS:
            ais[a] = {"session": bool(meta.get(a + "_session")), "handed_off": meta["handed_off"][a],
                      "busy": busy_for(a), "cmd": handoff_cmd(a), "model": model_info(a),
                      "ctx": meta["ctx"].get(a)}
        return {"meeting": mid_now(), "title": meta["title"], "status": meta["status"],
                "archived": is_archived(meta["status"]),
                "announcement": meta["announcement"], "ann_ver": meta["ann_ver"], "ais": ais,
                "board": meta["board"], "workdir": workdir(), "default_workdir": PROJECT, "stop_each": True,
                "mode": meta.get("mode", "discuss"),
                "steps_undone": [int(k) for k, v in meta["steps"].items() if v.get("undone")],
                "running": list(state["running"]), "queued": len(pending) + state["compact_q"]}


USER_ONLY_PATHS = ("/api/send", "/api/upload", "/api/announcement", "/api/mode", "/api/undo", "/api/undo/preview",
                   "/api/meetings/new", "/api/meetings/open", "/api/handoff", "/api/compact", "/api/wrapup", "/api/stop", "/api/board",
                   "/api/meetings/archive", "/api/meetings/delete", "/api/trash/restore", "/api/trash/purge", "/api/models")


class Handler(BaseHTTPRequestHandler):
    server_version = "Roundtable"

    def log_message(self, *a):
        pass

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

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

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
        kind = kind_of(full)
        safe = kind in ("image", "audio") or ctype == "application/pdf"
        if ctype == "image/svg+xml":
            safe = False
        total = os.path.getsize(full)
        start, end, code = 0, total - 1, 200
        rg = self.headers.get("Range", "")
        mt = re.match(r"bytes=(\d*)-(\d*)$", rg.strip())
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

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                html = f.read().replace(b"__RT_TOKEN__", RT_TOKEN.encode("ascii"))
            self._send(200, html, "text/html; charset=utf-8")
        elif path.startswith("/files/"):
            self._serve_file(path[len("/files/"):])
        elif path == "/api/announcement":
            self._json(200, {"text": read_announcement()})
        elif path == "/api/state":
            self._json(200, api_state())
        elif path == "/api/meetings":
            self._json(200, {"meetings": list_meetings()})
        elif path == "/api/trash":
            self._json(200, {"trash": list_trash()})
        elif path == "/api/messages":
            since = 0
            for kv in qs.split("&"):
                if kv.startswith("since="):
                    try:
                        since = max(0, int(kv[6:]))
                    except ValueError:
                        pass
            with lock:
                self._json(200, {"meeting": mid_now(), "messages": messages[since:], "next": len(messages),
                                 "running": list(state["running"]), "queued": len(pending) + state["compact_q"]})
        else:
            self._send(404, b"not found", "text/plain")

    do_HEAD = do_GET

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
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            self._json(400, {"error": "bad json"})
            return None

    def _from_browser(self):
        """代表用户的操作只认浏览器：Sec-Fetch-Site: same-origin，或页面注入的 X-RT-Token。
        这是防误用，不是防故意伪造（curl 想伪造请求头是做得到的）。"""
        if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "same-origin":
            return True
        tok = self.headers.get("X-RT-Token") or ""
        return bool(tok) and hmac.compare_digest(tok.encode("utf-8"), RT_TOKEN.encode("utf-8"))

    def do_POST(self):
        path, _, qs = self.path.partition("?")
        if path == "/api/post":
            data = self._read_json()
            if data is None:
                return
            if not isinstance(data, dict):
                return self._json(400, {"error": "bad json"})
            speaker = str(data.get("speaker") or "").strip()
            if speaker not in EXTERNAL_SPEAKERS:
                return self._json(400, {"error": "speaker 必填，且只能是：" + "、".join(EXTERNAL_SPEAKERS)})
            if meta is None:
                return self._json(409, {"error": "还没有会议，先开一个"})
            text = str(data.get("text", "")).strip()
            if not text:
                return self._json(400, {"error": "empty"})
            handle_message(speaker, text, origin="external")
            return self._json(200, {"ok": True, "speaker": speaker, "origin": "external"})
        if path in USER_ONLY_PATHS and not self._from_browser():
            return self._json(403, {"error": USER_ONLY_MSG})
        if path == "/api/upload":
            if meta is None:
                return self._json(409, {"error": "还没有会议"})
            n = self._length()
            if n <= 0:
                return self._json(400, {"error": "empty"})
            if n > MAX_UPLOAD:
                return self._json(413, {"error": "too large (max 25MB)"})
            name = (parse_qs(qs).get("name") or [""])[0]
            try:
                full = save_upload(name, self.rfile.read, n)
            except Exception as e:
                return self._json(500, {"error": str(e)})
            return self._json(200, make_attachment(full))
        if path == "/api/announcement":
            data = self._read_json()
            if data is None:
                return
            if meta is None:
                return self._json(409, {"error": "还没有会议"})
            text = str(data.get("text", ""))
            if len(text.encode("utf-8")) > MAX_ANN:
                return self._json(413, {"error": "too large (max 16KB)"})
            write_announcement(text)
            return self._json(200, {"ok": True})
        if path == "/api/board":
            data = self._read_json()
            if data is None:
                return
            if meta is None:
                return self._json(409, {"error": "还没有会议"})
            op = str(data.get("op", ""))
            try:
                ids = [int(data.get("id"))] if data.get("id") is not None else []
            except (TypeError, ValueError):
                return self._json(400, {"error": "bad id"})
            r = board_op("你", op, str(data.get("kind", "")), str(data.get("text", "")), ids)
            if not r:
                return self._json(400, {"error": "没改成"})
            board_note("你", [r])
            return self._json(200, {"ok": True})
        if path == "/api/send":
            data = self._read_json()
            if data is None:
                return
            if meta is None:
                return self._json(409, {"error": "还没有会议，先开一个"})
            text = str(data.get("text", "")).strip()
            atts = clean_attachments(data.get("attachments"))
            if not text and not atts:
                return self._json(400, {"error": "empty"})
            reply = make_reply(data.get("reply_to"), data.get("reply_part")) if data.get("reply_to") is not None else None
            handle_message("你", text, atts, reply_to=reply)
            return self._json(200, {"ok": True})
        if path in ("/api/mode", "/api/undo", "/api/undo/preview"):
            data = self._read_json()
            if data is None:
                return
            if path == "/api/mode":
                ok, msg = set_mode(str(data.get("mode", "")))
            else:
                try:
                    seq = int(data.get("step"))
                except (TypeError, ValueError):
                    return self._json(400, {"ok": False, "error": "bad step"})
                if path == "/api/undo/preview":
                    ok, msg, plan = undo_preview(seq)
                    return self._json(200 if ok else 409, {"ok": ok, "error": msg, "plan": plan})
                ok, msg = undo_step(seq)
            return self._json(200 if ok else 409, {"ok": ok, "error": msg})
        if path in ("/api/meetings/archive", "/api/meetings/delete", "/api/trash/restore", "/api/trash/purge", "/api/models"):
            data = self._read_json()
            if data is None:
                return
            if not isinstance(data, dict):
                return self._json(400, {"error": "bad json"})
            if path == "/api/models":
                ok, msg, changed = set_models(str(data.get("ai", "")), str(data.get("model") or ""), str(data.get("effort") or ""))
                return self._json(200 if ok else 409, {"ok": ok, "error": msg, "model_changed": changed})
            if path == "/api/meetings/archive":
                ok, msg = set_archived(str(data.get("id", "")), bool(data.get("on")))
            elif path == "/api/meetings/delete":
                ok, msg = trash_meeting(str(data.get("id", "")))
            elif path == "/api/trash/restore":
                ok, msg, _new = restore_meeting(str(data.get("id", "")))
            else:
                ok, msg = purge_all()
            return self._json(200 if ok else 409, {"ok": ok, "error": msg})
        if path in ("/api/meetings/new", "/api/meetings/open", "/api/handoff", "/api/compact", "/api/wrapup", "/api/stop"):
            data = self._read_json()
            if data is None:
                return
            if path == "/api/wrapup":
                ok, msg = request_wrapup()
                return self._json(200 if ok else 409, {"ok": ok, "error": msg})
            if path == "/api/stop":
                who = str(data.get("ai") or "") if isinstance(data, dict) else ""
                if who and who not in AIS:
                    return self._json(400, {"error": "ai 只能是 claude / codex，或者不填（全停）"})
                return self._json(200, {"ok": True, "msg": stop_ais([who] if who else AIS)})
            if path == "/api/compact":
                if meta is None:
                    return self._json(409, {"error": "还没有会议"})
                request_compact()
                return self._json(200, {"ok": True})
            if path == "/api/handoff":
                ok, msg = set_handoff(str(data.get("ai", "")), bool(data.get("on")))
                return self._json(200 if ok else 409, {"ok": ok, "error": msg})
            if busy_for():
                return self._json(409, {"error": "AI 还在回复，等它们说完再切换会议"})
            if path == "/api/meetings/new":
                ann = str(data.get("announcement", ""))
                if len(ann.encode("utf-8")) > MAX_ANN:
                    return self._json(413, {"error": "too large (max 16KB)"})
                wd = str(data.get("workdir") or "").strip().strip('"') or PROJECT
                wd = os.path.normpath(os.path.expandvars(os.path.expanduser(wd)))
                if not os.path.isabs(wd) or not os.path.isdir(wd):
                    return self._json(400, {"error": "工作文件夹不存在：" + wd})
                mid = new_meeting(str(data.get("title", "")), ann, wd)
                if mid is None:
                    return self._json(409, {"error": "AI 还在回复，等它们说完再切换会议"})
                return self._json(200, {"ok": True, "id": mid})
            ok, msg = open_meeting(str(data.get("id", "")))
            if not ok:
                return self._json(404 if msg == "没有这场会议" else 409, {"error": msg})
            return self._json(200, {"ok": True})
        self._send(404, b"not found", "text/plain")


def main():
    load_current()
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = "http://%s:%d/" % (HOST, PORT)
    print("会议室已启动：" + url + ("" if meta else "（还没有会议，在页面里开一个）"))
    if os.environ.get("ROUNDTABLE_NO_BROWSER") != "1":
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
