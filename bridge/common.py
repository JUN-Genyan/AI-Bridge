# -*- coding: utf-8 -*-
"""路径、上限、规矩文本、小工具。别的模块都从这里拿，测试改环境变量要在 import 之前。"""
import hashlib, json, os, re, subprocess, threading, time
from datetime import datetime

HOST = "127.0.0.1"
PORT = int(os.environ.get("ROUNDTABLE_PORT", "8765"))
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根（index.html 在这里）
LOG_DIR = os.environ.get("ROUNDTABLE_LOG_DIR", os.path.join(HERE, "data"))   # 会议记录、附件、私有 index；不进 git
# 默认工作文件夹：老会议没记工作文件夹时用它，新会议弹窗没东西可带时也用它
PROJECT = os.path.normpath(os.environ.get("ROUNDTABLE_DEFAULT_DIR") or os.path.expanduser("~"))
# 附件路径相对谁记：会议记录目录在默认工作文件夹里就相对它（最早的记录都是这么存的），不在就相对会议记录目录（跨盘也不出错）。
# 现在的记录都是相对会议记录目录的（meetings/<会议>/files/…）；公式别改，改了老记录里的附件就找不到了
ATT_BASE = PROJECT if os.path.normcase(os.path.abspath(LOG_DIR)).startswith(os.path.normcase(os.path.abspath(PROJECT)) + os.sep) else LOG_DIR
MEET_DIR = os.path.join(LOG_DIR, "meetings")
TRASH_DIR = os.path.join(LOG_DIR, "trash")
LAST_FILE = os.path.join(LOG_DIR, "current.txt")   # 最近打开的会议（外部程序没指定会议时发到这里；页面没带会议号时打开它）

MAX_BODY = 64 * 1024
MAX_UPLOAD = 25 * 1024 * 1024
MAX_ANN = 16 * 1024
MAX_ATTACH = 10
MAX_IMAGES = 5
TIMEOUT = 600          # 会议室自己跑的小命令（压缩之类）
# AI 一轮：多久没有任何输出就停（真卡死），加一个总上限。2026-10-15 claude 本机出图等了 30 分钟被总时长掐掉，所以改成按"没动静"算。
IDLE_LIMIT = {"discuss": 600, "work": 1200}
TOTAL_LIMIT = {"discuss": 1800, "work": 7200}
MODES = ("discuss", "work")
AIS = ("claude", "codex")
# 外部程序经 /api/post 或 MCP 只能以 claude / codex 身份发言（会议室外的同名 AI，消息带 origin=external）。
# 你 / 系统 不在内：你 = 只能来自网页上用户的操作；系统 = 会议室自己
EXTERNAL_SPEAKERS = AIS
SID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z\-]{7,63}$")   # 会话 id 只能是这种字符，防止拼路径拼出别的地方
ROOM_RE = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,120}$")   # 会议目录名（会议号）

# ===== 规矩（前言）=====
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
    "这场会议有一个目标（用户写的，随时会改），以它为准安排你的事。"
    "同一时间可能还有别的会议室在跑，别的会议里的 AI 也可能在同一个文件夹里干活。\n"
    "附件给的是本机路径，用读文件工具（Read）去看，图片也可以直接读。"
)

MODE_DISCUSS = (
    "【当前是讨论模式】你只能看文件，不能改任何东西；要改的话让用户切到干活模式，或回各自的会话去做。"
    "讨论模式只接会议室自己的 bridge 工具，别的 MCP 工具不加载。codex 注意：命令行能用来读文件，但在只读沙箱里，写不进任何东西。"
)

MODE_WORK = (
    "【当前是干活模式】你有完整权限，跟用户平时在桌面版 / 终端里用你一样：能改文件、跑命令、联网、用 git、用技能，"
    "用户给这场会议勾选的 MCP 工具也加载了。规矩：\n"
    "1. 动手前先 @ 对方说清要改哪些文件，别和对方同时改同一个文件。\n"
    "2. 删文件、git commit / push / reset、装软件、动项目外的东西，这类撤不回的事先在会议里说清楚要做什么，用户没反对再做。\n"
    "3. 会议室按你自己的写文件操作（claude 的 Edit/Write，codex 的 apply_patch）记录每一步改了哪些文件，用户能一键撤回；"
    "用命令行写的文件不在记录里，撤不回，也躲过了文件护栏。能用编辑工具改的就别用命令写。\n"
    "4. 改完 @用户 简短汇报：改了什么、哪些文件。\n"
    "文件护栏：你用编辑工具第一次改某个文件时，会议室自动替你占住它，这一轮说完自动放开。"
    "别人（这场会议的另一个 AI，或别的会议室里的 AI）占着的文件你改不了，会被挡下并告诉你是谁在改："
    "被挡了就先做别的，或 @ 对方 / @用户 商量，绝不要换成命令行去写它。"
    "要连着几轮改同一批文件，先用 bridge 的 claim_files 占住（写清原因、大概多久），做完用 release_files 放开。"
)

BRIDGE_RULES = (
    "会议室给你接了一个 MCP 服务 bridge：room_info 看这场会议的目标、看板、谁占着哪些文件；"
    "post 在一轮中途往会议里发一句进度、或者发文件给用户看（不叫醒别人，跑很久的活用它报进度）；"
    "board 改看板；claim_files / release_files / list_claims 占文件（干活模式用）。"
    "bridge 不通时照下面的写法在回复里写，效果一样。"
)

BOARD_RULES = (
    "看板：会议室里有一块看板（公告 + 待办），用户和你们都能写。要写就在回复里单独起一行：\n"
    "【公告】内容 —— 贴一条公告；【待办】内容 —— 加一条待办；"
    "【完成】编号 —— 勾掉待办（几个编号用空格隔开）；【删】编号 —— 删掉一条。\n"
    "这几行会被会议室收走，不显示在聊天里，改动会以系统消息告诉大家。只记真要跟踪的事，别刷屏。"
)

SEND_RULES = (
    "发图片 / 文件：想把本机上的图片或文件直接给用户看，就在回复里单独起一行：【发送】文件路径"
    "（完整路径，或相对工作目录的路径；一行一个，一次最多 %d 个，单个 %d MB 内）。"
    "这行会被会议室收走，文件复制进会议附件、挂在你这条消息上，图片在页面上直接显示。"
    "没发成会有系统消息说明原因。别只写路径让用户自己去找。"
    "已经知道是哪个文件、只是发给用户时，直接写【发送】，不用先打开看（打开只是你自己看到，用户看不到，还白占上下文）；"
    "要靠看内容才能挑出来时照常看，但先用文件名和文件夹缩小范围，一次少看几张。"
    "说“出了一张图”“做好了”的时候，就用【发送】把它发出来，别只描述。\n"
    "出图：codex 自带生图（不占本机显卡，用户账号的额度），出概念图、氛围图优先让 codex 用它，生成的图会议室会自动发出来。"
    "本机显卡（ComfyUI 出图、渲染、训练这类重活）同一时间只能一个人用：动手前先 @ 对方说一声，对方在跑就等它跑完，别同时往上提交。\n"
    "跑很久的活：先说一句在干什么、大概要多久。会议室 %d 分钟（讨论模式 %d 分钟）收不到你任何动静就会把你停掉，"
    "等后台任务时隔一阵说一句进度，别一声不吭等太久；真要等很久就先结束这一轮，@用户 说清楚怎么接着看结果。"
    % (MAX_ATTACH, MAX_UPLOAD // (1024 * 1024), IDLE_LIMIT["work"] // 60, IDLE_LIMIT["discuss"] // 60)
)


def mode_text(mode):
    return (MODE_WORK if mode == "work" else MODE_DISCUSS) + "\n" + BRIDGE_RULES + "\n" + BOARD_RULES + "\n" + SEND_RULES


def mode_key(mode):
    """模式 + 规矩文本的指纹。规矩改了（比如干活模式放开），已有会话下一轮也会收到新版。"""
    return mode + ":" + hashlib.sha1(mode_text(mode).encode("utf-8")).hexdigest()[:8]


def preamble(mode, wd):
    return PREAMBLE_BASE.replace("{wd}", wd) + "\n" + mode_text(mode)


def timeout_for(mode):
    """返回 (多久没动静就停, 一轮总上限)，单位秒。"""
    m = mode if mode in IDLE_LIMIT else "discuss"
    return IDLE_LIMIT[m], TOTAL_LIMIT[m]


def timeout_text(ai, why, mode):
    idle, total = timeout_for(mode)
    if why == "idle":
        return "%s 超时了：%d 分钟没有任何动静，像是卡住了，已经停掉" % (ai, idle // 60)
    return "%s 超时了：这一轮超过 %d 分钟，已经停掉" % (ai, total // 60)


# ===== 小工具 =====
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def safe_title(t):
    t = re.sub(r'[\/:*?"<>|\x00-\x1f]', "", t or "")
    t = re.sub(r"\s+", "_", t.strip()).strip("._")
    return t[:40] or "会议"


def ref_safe(name):
    n = re.sub(r"[^\w\-]", "_", name, flags=re.UNICODE)
    return n.strip("_.") or "m"


def is_archived(status):
    return status in ("archived", "closed")   # 老数据的 closed 读作已归档


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


def norm(p):
    return os.path.normcase(os.path.realpath(p))


def in_dir(full, base):
    full, base = norm(full), norm(base)
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


NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def resolve_cmd(name):
    """命令的真身，返回参数列表的开头（找不到返回 None）。
    Windows 上 npm 装的 claude / codex 是 .cmd 外壳：经它起会过一道 cmd.exe，参数里的 | & < > ^（钩子配置、会议名）会被当成命令符号，
    2026-10-22 实测 --settings 里的 "Edit|Write" 被拆成了管道。所以能找到真身就直接起真身：
    外壳里写的是 node 脚本（codex.js）就用 node 跑它；写的是 .exe（claude.exe）就直接起它。"""
    import shutil
    p = shutil.which(name)
    if not p:
        return None
    if os.name != "nt" or not p.lower().endswith((".cmd", ".bat")):
        return [p]
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return [p]
    d = os.path.dirname(p)
    m = re.search(r'"%dp0%\\([^"]+?\.js)"', txt, re.I)
    if m:
        js = os.path.join(d, m.group(1))
        node = os.path.join(d, "node.exe")
        node = node if os.path.isfile(node) else shutil.which("node")
        if node and os.path.isfile(js):
            return [node, js]
    m = re.search(r'"%dp0%\\([^"]+?\.exe)"', txt, re.I)
    if m:
        exe = os.path.join(d, m.group(1))
        if os.path.isfile(exe):
            return [exe]
    return [p]


def kill_tree(p):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, timeout=30, **NO_WINDOW)
        else:
            p.kill()
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def run_cmd(cmd, prompt, cwd, timeout=TIMEOUT):
    """一次跑完的小命令（压缩之类）。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=cwd, env=env, timeout=timeout, **NO_WINDOW)


def run_stream(cmd, prompt, on_line=None, timeout=(TIMEOUT, TIMEOUT), cwd=None, env=None, reg=None):
    """边跑边逐行读 stdout（让 on_line 能在 AI 中途就拿到它的操作记录；超时/崩了也不丢）。
    timeout = (多久没输出就停, 总上限)。env = 额外的环境变量。reg(进程) 在起来后马上被调一次（"停"的时候要杀它）。
    返回 (退出码, stdout 全文, stderr 全文, 超时原因 None / "idle" / "total")。"""
    idle, total = timeout if isinstance(timeout, tuple) else (timeout, timeout)
    t0, last = time.time(), [time.time()]
    full_env = dict(os.environ, PYTHONIOENCODING="utf-8")
    full_env.update(env or {})
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=cwd, env=full_env, **NO_WINDOW)
    if reg:
        reg(p)
    lines, errs = [], []

    def rd_out():
        for raw in p.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            last[0] = time.time()
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
    timed_out = None
    while p.poll() is None:
        now = time.time()
        if now - last[0] > idle:
            timed_out = "idle"
        elif now - t0 > total:
            timed_out = "total"
        if timed_out:
            break
        time.sleep(1)
    if timed_out:
        kill_tree(p)
        try:
            p.wait(timeout=15)
        except Exception:
            pass
    for t in ts:
        t.join(10)
    return p.returncode, "\n".join(lines), b"".join(errs).decode("utf-8", "replace"), timed_out
