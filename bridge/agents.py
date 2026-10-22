# -*- coding: utf-8 -*-
"""怎么叫两个 AI。

claude：每轮起一个 claude -p --resume <会话>（stream-json 逐行读）。这场会议的设置（模型、MCP、插件、技能、护栏钩子）都在命令行上带着。
codex：每场会议一个常驻的 codex app-server（stdio 上收发 JSON-RPC），这场会议的设置在启动参数里；
       每轮 thread/resume（或新建）→ turn/start → 等 turn/completed → thread/unsubscribe。设置变了（换模式、换工具）就重起它。
两边都接会议室自己的 MCP 服务 bridge（/mcp），用这场会议给它的钥匙证明身份。"""
import glob, json, os, queue, re, shutil, subprocess, sys, tempfile, threading, time, uuid
from datetime import datetime

from .common import (AIS, LOG_DIR, NO_WINDOW, PORT, PROJECT, TIMEOUT, in_dir, kill_tree, norm, read_json,
                     resolve_cmd, run_cmd, run_stream, timeout_for, timeout_text)
from .guard import block_note, block_text, guard

HOME = os.path.expanduser("~")
CLAUDE_HOME = os.path.join(HOME, ".claude")                  # 测试时可替换
CLAUDE_SETTINGS = os.path.join(CLAUDE_HOME, "settings.json")
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
CODEX_CONFIG = os.path.join(CODEX_HOME, "config.toml")


def bridge_url(path=""):
    return "http://127.0.0.1:%d%s" % (PORT, path)


# ===================== 模型 / effort =====================
CLAUDE_MODEL_PRESETS = ["opus", "sonnet", "haiku", "fable"]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]   # claude --help 里 --effort 支持的值
CODEX_EFFORTS_DEFAULT = ["low", "medium", "high"]
MODEL_RE = re.compile(r"^[A-Za-z0-9][\w.\-:/\[\]]{0,79}$")
EFFORT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,20}$")
# 会议没自己设时用的模型 / effort（用户 2026-10-16 定），不再跟随 ~/.claude、~/.codex 的全局设置
ROOM_DEFAULTS = {"claude": ("claude-opus-5-5", "medium"), "codex": ("gpt-6.1-sol", "medium")}


def global_claude(for_model=None):
    """只读 settings.json 的 model；effort 查 modelSettings.<模型>.effortLevel（for_model 缺省 = 全局 model）。读不到的是 ''。"""
    model = effort = ""
    s = read_json(CLAUDE_SETTINGS, {}) or {}
    if isinstance(s, dict):
        model = s.get("model") if isinstance(s.get("model"), str) else ""
        key = for_model or model
        ms = s.get("modelSettings")
        if key and isinstance(ms, dict):
            hit = ms.get(key)
            if not isinstance(hit, dict):   # 别名（opus）对不上全名（claude-opus-4-8）时，按"全名里含别名"找
                hit = next((v for k, v in ms.items() if isinstance(v, dict) and key.lower() in k.lower()), None)
            if isinstance(hit, dict) and isinstance(hit.get("effortLevel"), str):
                effort = hit["effortLevel"]
    return model, effort


def codex_cfg():
    try:
        import tomllib
        with open(CODEX_CONFIG, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def global_codex():
    c = codex_cfg()
    model = c.get("model") if isinstance(c.get("model"), str) else ""
    effort = c.get("model_reasoning_effort") if isinstance(c.get("model_reasoning_effort"), str) else ""
    return model, effort


def codex_catalog():
    """codex 自己缓存的模型清单（~/.codex/models_cache.json），只取 visibility=list 的。[(slug, [effort...])]"""
    c = read_json(os.path.join(os.path.dirname(CODEX_CONFIG), "models_cache.json"))
    ms = c.get("models", []) if isinstance(c, dict) else (c or [])
    out = []
    for m in ms if isinstance(ms, list) else []:
        if isinstance(m, dict) and m.get("slug") and m.get("visibility", "list") == "list":
            lv = [x.get("effort") if isinstance(x, dict) else x for x in (m.get("supported_reasoning_levels") or [])]
            out.append((m["slug"], [x for x in lv if isinstance(x, str)]))
    return out


def effort_options(ai, model=None):
    if ai == "claude":
        return list(CLAUDE_EFFORTS)
    cur = model or ROOM_DEFAULTS["codex"][0]
    for slug, lv in codex_catalog():
        if slug == cur and lv:
            return lv
    c = codex_cfg()
    v = c.get("enabled-reasoning-efforts")
    if not isinstance(v, list):
        v = (c.get("desktop") or {}).get("enabled-reasoning-efforts") if isinstance(c.get("desktop"), dict) else None
    v = [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    return v or list(CODEX_EFFORTS_DEFAULT)


def model_options(ai):
    out = list(CLAUDE_MODEL_PRESETS) if ai == "claude" else [s for s, _ in codex_catalog()]
    d = ROOM_DEFAULTS[ai][0]
    if d not in out:
        out.insert(0, d)
    return out


# ===================== 这场会议的 MCP / 插件 / 技能 =====================
# 存法：mcp = 勾上的 MCP 服务名（默认一个不勾：回得快；只在干活模式加载）；
#       plugins = 这场会议对插件的开关 {插件 id: 开/关}，没写的按默认规则；
#       skills_off = 这场会议关掉的技能（默认空 = 跟用户平时一样）。
# 插件的默认规则：带钩子的（比如 superpowers 开场会往会话里塞一大段说明）、从 claude.ai 同步来的（slack / discord 之类）默认关，
# 别的跟用户平时一样。bridge（会议室自己的 MCP）两种模式都接，不在清单里。
NAME_RE = re.compile(r"^[\w.@:/+\-]{1,120}$")


def default_tools(ai):
    return {"mcp": [], "plugins": {}, "skills_off": []}


def fix_tools(ai, cfg):
    cfg = cfg if isinstance(cfg, dict) else {}
    out = default_tools(ai)
    for k in ("mcp", "skills_off"):
        v = cfg.get(k)
        out[k] = sorted({x for x in v if isinstance(x, str) and NAME_RE.match(x)}) if isinstance(v, list) else []
    v = cfg.get("plugins")
    if isinstance(v, dict):
        out["plugins"] = {k: bool(b) for k, b in sorted(v.items()) if isinstance(k, str) and NAME_RE.match(k)}
    for pid in cfg.get("plugins_off") or []:   # 早期写法
        if isinstance(pid, str) and NAME_RE.match(pid):
            out["plugins"][pid] = False
    return out


def plugin_on(p, cfg):
    """这场会议里这个插件开不开：会议自己设了听会议的；没设的，带钩子 / 同步来的关，别的跟用户平时一样。"""
    if p["id"] in cfg["plugins"]:
        return cfg["plugins"][p["id"]]
    if p.get("hooks") or p.get("synced"):
        return False
    return p["enabled"]


def tools_summary(ai, cfg):
    bits = ["MCP：" + ("、".join(cfg["mcp"]) + "（干活模式）" if cfg["mcp"] else "只有 bridge")]
    if cfg["plugins"]:
        on = sum(1 for v in cfg["plugins"].values() if v)
        bits.append("插件按会议设的（开 %d 关 %d）" % (on, len(cfg["plugins"]) - on))
    if cfg["skills_off"]:
        bits.append("关了 %d 个技能" % len(cfg["skills_off"]))
    return " · ".join(bits)


_disc_cache = {}
_disc_lock = threading.Lock()


def _cached(key, fn, ttl=60):
    with _disc_lock:
        hit = _disc_cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    v = fn()
    with _disc_lock:
        _disc_cache[key] = (time.time(), v)
    return v


def _skill_desc(p):
    """SKILL.md 开头 frontmatter 里的 description（截短）。"""
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            head = f.read(4000)
    except OSError:
        return ""
    m = re.search(r"^description:\s*(.+)$", head, re.M)
    d = (m.group(1).strip().strip("\"'") if m else "")
    return d[:160] + ("…" if len(d) > 160 else "")


def _skills_in(base, source):
    out = []
    for p in sorted(glob.glob(os.path.join(base, "*", "SKILL.md"))):
        out.append({"name": os.path.basename(os.path.dirname(p)), "source": source, "desc": _skill_desc(p)})
    return out


def _wd_match(key, wd):
    a = os.path.normcase(os.path.normpath(key.replace("/", os.sep))).rstrip("\\/")
    b = os.path.normcase(os.path.normpath(wd)).rstrip("\\/")
    return a == b


def claude_mcp_defs(wd):
    """claude 能用的 MCP 服务：{名字: (定义, 来源)}。用户级（~/.claude.json 顶层）、这个文件夹的本地级（~/.claude.json 的 projects）、
    项目级（文件夹里的 .mcp.json）。插件自带的 MCP 跟着插件开关走，不在这里。"""
    out = {}
    cj = read_json(CLAUDE_JSON, {}) or {}
    for n, d in (cj.get("mcpServers") or {}).items():
        if isinstance(d, dict):
            out[n] = (d, "用户级")
    for k, v in (cj.get("projects") or {}).items():
        if isinstance(v, dict) and _wd_match(k, wd):
            for n, d in (v.get("mcpServers") or {}).items():
                if isinstance(d, dict):
                    out[n] = (d, "这个文件夹")
    pj = read_json(os.path.join(wd, ".mcp.json"), {}) or {}
    for n, d in (pj.get("mcpServers") or {}).items():
        if isinstance(d, dict):
            out[n] = (d, "项目 .mcp.json")
    out.pop("bridge", None)
    return out


def _plugin_has_hooks(path):
    if not path:
        return False
    if os.path.isfile(os.path.join(path, "hooks", "hooks.json")):
        return True
    pj = read_json(os.path.join(path, ".claude-plugin", "plugin.json"), {}) or {}
    return bool(pj.get("hooks"))


def claude_plugins(wd):
    """这个文件夹里 claude 能用的插件：[{id, enabled（用户平时开没开）, hooks, synced}]。用 claude plugin list --json（准），不行就读文件。"""
    exe = resolve_cmd("claude")
    if exe:
        try:
            r = run_cmd(exe + ["plugin", "list", "--json"], "", wd, timeout=60)
            data = json.loads(r.stdout) if r.returncode == 0 else None
        except Exception:
            data = None
        if isinstance(data, list):
            out = []
            for x in data:
                if isinstance(x, dict) and isinstance(x.get("id"), str) and NAME_RE.match(x["id"]):
                    if x.get("scope") == "synced" and not x.get("installPath"):
                        continue   # 被本机同名插件盖住的，不会加载
                    out.append({"id": x["id"], "enabled": bool(x.get("enabled")), "hooks": _plugin_has_hooks(x.get("installPath") or ""),
                                "synced": x.get("scope") == "synced", "path": x.get("installPath") or ""})
            return out
    ip = read_json(os.path.join(CLAUDE_HOME, "plugins", "installed_plugins.json"), {}) or {}
    en = {}
    for f in (CLAUDE_SETTINGS, os.path.join(wd, ".claude", "settings.json"), os.path.join(wd, ".claude", "settings.local.json")):
        e = (read_json(f, {}) or {}).get("enabledPlugins")
        if isinstance(e, dict):
            en.update(e)
    out = []
    for pid, v in (ip.get("plugins") or {}).items():
        e = v[0] if isinstance(v, list) and v else (v if isinstance(v, dict) else {})
        path = e.get("installPath") or ""
        out.append({"id": pid, "enabled": bool(en.get(pid)), "hooks": _plugin_has_hooks(path), "synced": False, "path": path})
    return out


def claude_skills(wd, plugins=None):
    """个人 / 项目技能（能一个一个关）+ 插件带的技能（跟着插件开关走）。"""
    out = _skills_in(os.path.join(CLAUDE_HOME, "skills"), "个人")
    out += _skills_in(os.path.join(wd, ".claude", "skills"), "项目")
    for p in plugins or []:
        if p.get("path"):
            for sk in _skills_in(os.path.join(p["path"], "skills"), "插件"):
                sk["plugin"] = p["id"]
                out.append(sk)
    return out


def codex_mcp_names():
    """~/.codex/config.toml 里配的全部 MCP 服务名。名字只认字母数字下划线横杠，别的拼不进 -c。"""
    return [n for n in ((codex_cfg().get("mcp_servers")) or {}).keys() if re.match(r"^[\w\-]+$", n) and n != "bridge"]


def codex_mcp_off_by_config():
    return [n for n, d in ((codex_cfg().get("mcp_servers")) or {}).items() if isinstance(d, dict) and d.get("enabled") is False]


def codex_skills(wd):
    """codex 的技能：问一个临时的 app-server（skills/list，准：含系统技能、同步来的、插件带的）；不行就读文件夹。"""
    try:
        cmd, env = codex_launch(None, "discuss")
        srv = CodexServer(cmd, env)
        try:
            srv.start()
            r = srv.request("skills/list", {"cwds": [wd], "forceReload": False}, timeout=60)
        finally:
            srv.stop()
        out, seen = [], set()
        for d in r.get("data") or []:
            for sk in d.get("skills") or []:
                n = sk.get("name")
                if not isinstance(n, str) or n in seen or not NAME_RE.match(n):
                    continue
                seen.add(n)
                x = {"name": n, "source": {"user": "个人", "repo": "项目", "system": "系统", "admin": "管理员"}.get(sk.get("scope"), sk.get("scope") or ""),
                     "desc": (sk.get("shortDescription") or sk.get("description") or "")[:160]}
                if sk.get("pluginId"):
                    x["plugin"] = "*"   # 插件带的：跟着"全部插件"开关走
                out.append(x)
        if out:
            return out
    except Exception:
        pass
    out = _skills_in(os.path.join(CODEX_HOME, "skills"), "个人")
    out += _skills_in(os.path.join(HOME, ".agents", "skills"), "个人")
    out += _skills_in(os.path.join(wd, ".agents", "skills"), "项目")
    out += _skills_in(os.path.join(wd, ".codex", "skills"), "项目")
    seen, uniq = set(), []
    for sk in out:
        if sk["name"] not in seen:
            seen.add(sk["name"])
            uniq.append(sk)
    return uniq


def codex_plugins():
    """codex 的插件只能一起开关（2026-10-22 实测：按个关 -c plugins."x".enabled=false 对远程插件没用，只有 --disable plugins 管用），
    所以只给一个开关，id 记成 "*"。"""
    return [{"id": "*", "label": "全部插件（只能一起开关）", "enabled": True, "hooks": False, "synced": False, "path": ""}]


def prewarm(wd):
    """会议读进来时在后台先把 claude 的插件清单查好（claude plugin list 要一两秒），第一次叫 claude、打开工具栏时不用等。"""
    try:
        _cached(("cplug", wd), lambda: claude_plugins(wd))
    except Exception:
        pass


def tools_info(ai, cfg, wd):
    """胶囊弹窗里"工具"那一栏：能选的 MCP、插件、技能，各自在这场会议里开没开。"""
    if ai == "claude":
        defs = _cached(("cmcp", wd), lambda: claude_mcp_defs(wd), 20)
        plugs = _cached(("cplug", wd), lambda: claude_plugins(wd))
        skills = _cached(("cskill", wd), lambda: claude_skills(wd, plugs))
        mcp = [{"name": n, "on": n in cfg["mcp"], "source": src} for n, (d, src) in sorted(defs.items())]
    else:
        off_cfg = codex_mcp_off_by_config()
        mcp = [{"name": n, "on": n in cfg["mcp"], "source": "config.toml" + ("（配置里关着）" if n in off_cfg else "")}
               for n in _cached("xmcp", codex_mcp_names, 20)]
        plugs = _cached("xplug", codex_plugins)
        skills = _cached(("xskill", wd), lambda: codex_skills(wd))
    plugins = [{"id": p["id"], "label": p.get("label", ""), "on": plugin_on(p, cfg), "global": p["enabled"], "hooks": p.get("hooks", False),
                "synced": p.get("synced", False), "set": p["id"] in cfg["plugins"]} for p in plugs]
    on_plugins = {p["id"] for p in plugins if p["on"]}
    sk = []
    for x in skills:
        d = dict(x)
        if x.get("plugin"):
            d["on"] = x["plugin"] in on_plugins
            d["locked"] = True   # 插件带的技能跟着插件开关走，不能单个关
        else:
            d["on"] = x["name"] not in cfg["skills_off"]
        sk.append(d)
    return {"ai": ai, "mcp": mcp, "plugins": plugins, "skills": sk, "cfg": cfg}


# ===================== claude =====================
CLAUDE_READ = "Read,Grep,Glob"
CLAUDE_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
named = {}   # (ai, 会话 id) -> 已经设过的会话名；名字没变不重复设（claude 每设一次会往会话文件里追加一行）


def claude_mcp_config(room, mode, key):
    """--mcp-config 的内容：bridge（带钥匙）+ 干活模式下这场会议勾上的 MCP。"""
    servers = {"bridge": {"type": "http", "url": bridge_url("/mcp"), "headers": {"Authorization": "Bearer " + key}}}
    if mode == "work" and room is not None:
        defs = claude_mcp_defs(room.workdir())
        for n in room.meta["tools"]["claude"]["mcp"]:
            if n in defs:
                servers[n] = defs[n][0]
    return json.dumps({"mcpServers": servers}, ensure_ascii=False)


def claude_settings(room, mode, key):
    """--settings 的内容：
    - 文件护栏：PreToolUse 钩子，编辑文件前 POST 到会议室问一声（HTTP 钩子，不起子进程；bypassPermissions 下也照样拦）。
      会议室没回 / 出错时放行（护栏是防撞的，不该把 AI 卡死）。
    - 这场会议的插件开关、关掉的技能；不连 claude.ai 的连接器（回得快）。
    不再用 disableAllHooks：它会连我们自己的钩子一起关掉。用户自己的钩子目前只来自插件，带钩子的插件会议室默认关。"""
    s = {"hooks": {"PreToolUse": [{"matcher": "|".join(CLAUDE_EDIT_TOOLS),
                                    "hooks": [{"type": "http", "url": bridge_url("/api/guard/claude"), "timeout": 10,
                                               "headers": {"Authorization": "Bearer " + key}}]}]},
         "disableClaudeAiConnectors": True}
    if room is not None:
        cfg = room.meta["tools"]["claude"]
        wd = room.workdir()
        plugs = _cached(("cplug", wd), lambda: claude_plugins(wd))
        s["enabledPlugins"] = {p["id"]: plugin_on(p, cfg) for p in plugs}
        for pid, on in cfg["plugins"].items():   # 会议里设过、但这台机器上已经找不到的，照写不误
            s["enabledPlugins"].setdefault(pid, on)
        if cfg["skills_off"]:
            s["skillOverrides"] = {n: "off" for n in cfg["skills_off"]}
    return json.dumps(s, ensure_ascii=False)


def build_cmd(ai, mode, sid, exe, images=(), outf=None, model="", effort="", room=None, key=""):
    """拼出某个 AI 在某个模式下的命令行（新建会话 / resume 在这里分）。claude 新建会话时由调用方再补 --session-id。
    model / effort 空 = 不加参数。room 给了就带上这场会议的 MCP / 插件 / 技能 / 钩子设置。"""
    exe = list(exe) if isinstance(exe, (list, tuple)) else [exe]
    if ai == "claude":
        cmd = exe + ["-p"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if mode == "work":
            # 干活模式完全放开（用户 2026-10-13 定），跟平时用 Claude Code 一样；文件护栏靠 PreToolUse 钩子，bypass 下也照样拦
            cmd += ["--permission-mode", "bypassPermissions"]
        else:
            cmd += ["--allowedTools", CLAUDE_READ + ",mcp__bridge__room_info,mcp__bridge__list_claims,mcp__bridge__post,mcp__bridge__board"]
        # MCP：只接 bridge + 这场会议勾上的（干活模式）；用户平时的 MCP 不自动带上（回得快）
        cmd += ["--strict-mcp-config", "--mcp-config", claude_mcp_config(room, mode, key or "-"),
                "--settings", claude_settings(room, mode, key or "-")]
        if not in_dir(LOG_DIR, (room.workdir() if room is not None else PROJECT)):
            cmd += ["--add-dir", LOG_DIR]   # 工作文件夹不是本项目时，附件在会议室目录里，得让它能读
        cmd += ["--output-format", "stream-json", "--verbose"]   # 逐行事件：拿干净的最终回复，也拿它自己的写文件操作
        if sid:
            cmd += ["--resume", sid]
        return cmd
    sandbox = "workspace-write" if mode == "work" else "read-only"
    if sid:
        cmd = exe + ["exec", "resume", "--skip-git-repo-check", "-c", "sandbox_mode=" + sandbox, "--json"]
    else:
        cmd = exe + ["exec", "--sandbox", sandbox, "--skip-git-repo-check", "--color", "never", "--json"]
    if model:
        cmd += ["-m", model]   # exec 和 exec resume 都支持 -m
    if effort:
        cmd += ["-c", 'model_reasoning_effort="%s"' % effort]
    if mode == "work":
        cmd += ["-c", "sandbox_workspace_write.network_access=false"]
    # 两种模式都关 MCP：干活模式只留 codex 自带的 apply_patch，所有写入都进 --json 记录
    for srv in codex_mcp_names():
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
            if isinstance(c, dict) and c.get("type") == "text" and not j.get("parent_tool_use_id") and (c.get("text") or "").strip():
                res["live"] = c["text"]   # 中途说的话；页面状态行显示最近一句
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
    """codex --json 一行（旧的 codex exec 方式）：thread id、file_change 条目里的路径和类型、跑过的命令数。"""
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


def call_ai(ai, prompt, session_id=None, images=(), mode=None, room=None):
    """返回 (ok, text, session_id, edits)。session_id 为空 = 新建会话，返回新会话 id。mode 缺省取这场会议的模式。
    edits = 这个 AI 自己的写文件操作记录 [{path, op}]（干活模式靠它认定"这一步改了什么"）；失败/超时也尽量带上。"""
    edits, res = [], {}
    exe = resolve_cmd(ai)
    if not exe:
        return False, "找不到 %s 命令" % ai, session_id, edits
    if mode is None:
        mode = room.mode() if room is not None else "discuss"
    sid = session_id
    mo_m, mo_e = room.effective_models(ai) if room is not None else ROOM_DEFAULTS[ai]   # 没设也明着传会议室默认
    wd = room.workdir() if room is not None else PROJECT
    key = room.keys[ai] if room is not None else ""
    try:
        if ai == "claude":
            cmd = build_cmd(ai, mode, sid, exe, model=mo_m, effort=mo_e, room=room, key=key)
            if not sid:
                sid = str(uuid.uuid4())
                cmd += ["--session-id", sid]
            nm = room.session_name() if room is not None else ""
            if nm and named.get(("claude", sid)) != nm:
                cmd += ["-n", nm]   # 新会话起名；老会话 --resume 时也能改名
                named[("claude", sid)] = nm
            env = {"AIBRIDGE_URL": bridge_url(), "AIBRIDGE_KEY": key} if key else {}

            def reg(p):
                if room is not None:
                    room.procs[ai] = p

            def on_line(l):
                parse_claude_line(l, edits, res)
                if room is not None:
                    room.push_live(ai, res)

            rc, so, se, to = run_stream(cmd, prompt, on_line, timeout_for(mode), cwd=wd, env=env, reg=reg)
            r = res.get("result") or {}
            out = (r.get("result") or "").strip() if isinstance(r.get("result"), str) else ""
            bad = bool(r.get("is_error"))
            tail_src = (out if bad else "") or se or ""
        elif CODEX_BACKEND != "exec":
            # 会话里的模型 / effort 设一次会一直留着，所以每轮都明着传，免得沿用上一次的
            r = call_codex_appserver(room, prompt, sid, images, mode, mo_m, mo_e, edits, res)
            if r[0] and room is not None:
                room.note_ctx(ai, res)
            return r + (edits,)
        else:
            fd, outf = tempfile.mkstemp(suffix=".txt", prefix="rt_codex_")
            os.close(fd)
            try:
                cmd = build_cmd(ai, mode, sid, exe, images, outf, mo_m, mo_e)
                rc, so, se, to = run_stream(cmd, prompt, lambda l: parse_codex_line(l, edits, res), timeout_for(mode), cwd=wd,
                                            reg=(lambda p: room.procs.__setitem__(ai, p)) if room is not None else None)
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
            return False, timeout_text(ai, to, mode), session_id, edits
        if rc != 0 or bad:
            tail = "\n".join(tail_src.strip().splitlines()[-6:])
            return False, "%s 出错了（退出码 %s）\n%s" % (ai, rc, tail), session_id, edits
        if not out:
            return False, "%s 没有返回内容" % ai, session_id, edits
        if not sid:
            return False, "%s 没拿到会话 id" % ai, session_id, edits
        if room is not None:
            room.note_ctx(ai, res)
        return True, out, sid, edits
    except Exception as e:
        return False, "%s 调用失败：%s" % (ai, e), session_id, edits


def claude_boundaries(sid):
    """会话文件里 compact_boundary 的个数；找不到文件返回 None。"""
    for p in glob.glob(os.path.join(CLAUDE_HOME, "projects", "*", sid + ".jsonl")):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read().count('"compact_boundary"')
        except Exception:
            return None
    return None


def compact_native(ai, sid, room=None):
    """能真压缩就压缩，返回 True；这个 AI 不支持（codex exec 把 /compact 当普通文字）返回 False。"""
    if ai == "codex":
        if CODEX_BACKEND == "exec":
            return False
        srv = codex_for(room, room.mode() if room is not None else "discuss")
        try:
            return srv.compact(sid, room.workdir() if room is not None else PROJECT)
        except Exception:
            stop_codex(room)   # 下一轮自动重起
            raise
    if ai != "claude":
        return False
    exe = resolve_cmd("claude")
    if not exe:
        return False
    before = claude_boundaries(sid)
    try:
        r = run_cmd(exe + ["-p", "--resume", sid, "--allowedTools", CLAUDE_READ, "--output-format", "text",
                     "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--settings", '{"disableAllHooks":true}'],
                    "/compact", room.workdir() if room is not None else PROJECT)
    except Exception:
        return False
    if r.returncode != 0:
        return False
    after = claude_boundaries(sid)
    if before is None or after is None:
        return True
    return after > before


def delete_claude_session(sid):
    """删 claude 一个会话在本机的全部记录：projects/*/<id>.jsonl 和同名目录、file-history、session-env、todos。返回删掉的条数。"""
    from .common import SID_RE
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


# ===================== codex（App Server）=====================
# 旧的每轮起一个 codex exec 只能手动切回：环境变量 ROUNDTABLE_CODEX=exec
CODEX_BACKEND = os.environ.get("ROUNDTABLE_CODEX", "appserver")
# 实测（2026-10-13，codex-cli 0.162.1）：
# - elevated 沙箱每次跑命令前都要给桌面版运行时里的 node_repl.exe 刷权限，桌面版开着就被占住（os error 32），
#   跟 config 无关（换一份干净的 CODEX_HOME 也一样），所以会议室改用 unelevated。
# - unelevated 起不了商店版 pwsh（CreateProcessAsUserW 拒绝），所以子进程的 PATH 去掉 WindowsApps，
#   让 codex 退回系统自带的 Windows PowerShell。只读挡写、可写只限项目目录，都实测过。
CODEX_SANDBOX_OVERRIDE = 'windows.sandbox="unelevated"'
CODEX_DISCUSS_OFF = ("apps", "computer_use", "browser_use")   # 自带生图（image_generation）2026-10-15 起放开：不占本机显卡，生成的图会议室自动发出来
CODEX_IDLE_STOP = 20 * 60   # 一场会议的 app-server 闲了这么久就关掉（省内存），下一轮再起


def codex_env(extra=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                  if p and "windowsapps" not in p.lower())
    env.update(extra or {})
    return env


def codex_policy(mode):
    if mode == "work":
        return {"type": "dangerFullAccess"}   # 干活模式完全放开（用户 2026-10-13 定），跟平时用桌面版一样
    return {"type": "readOnly", "networkAccess": False}


def codex_launch(room, mode):
    """这场会议的 app-server 启动参数：返回 (命令行, 额外环境变量)。同样的参数 = 同一份配置，不用重起。"""
    exe = resolve_cmd("codex")
    if not exe:
        raise RuntimeError("找不到 codex 命令")
    cfg = room.meta["tools"]["codex"] if room is not None else default_tools("codex")
    cmd = exe + ["-c", CODEX_SANDBOX_OVERRIDE, "-c", "notify=[]"]
    for srv in codex_mcp_names():
        if not (mode == "work" and srv in cfg["mcp"]):
            cmd += ["-c", "mcp_servers.%s.enabled=false" % srv]   # 键名不能加引号（加了会被当成另一张表，codex 报 invalid transport）
    env = {}
    if room is not None:
        cmd += ["-c", 'mcp_servers.bridge.url="%s"' % bridge_url("/mcp"),
                "-c", 'mcp_servers.bridge.bearer_token_env_var="AIBRIDGE_KEY"']
        env = {"AIBRIDGE_KEY": room.keys["codex"], "AIBRIDGE_URL": bridge_url()}
    if not plugin_on(codex_plugins()[0], cfg):
        cmd += ["--disable", "plugins"]
    if cfg["skills_off"]:   # 技能能一个一个关
        cmd += ["-c", "skills.config=[%s]" % ",".join('{name="%s",enabled=false}' % n for n in cfg["skills_off"] if re.match(r"^[\w.\-]+$", n))]
    for ft in CODEX_DISCUSS_OFF:
        cmd += ["--disable", ft]
    if room is not None:
        cmd += codex_hook_args(exe)
    cmd += ["app-server", "--listen", "stdio://"]
    return cmd, env


# ----- codex 的文件护栏：PreToolUse 钩子拦 apply_patch -----
# codex 的钩子要"信任"过才跑：信任 = 启动参数里带上 hooks.state.<钥匙>.trusted_hash = 它算出来的 hash。
# hash 对同一个钩子命令是固定的（实测两次启动一样），所以第一次用一个临时的 app-server 查一次 hooks/list，存进会议记录目录，以后直接用。
_hook_lock = threading.Lock()
_hook_cache = {}


def codex_hook_cmd():
    py = sys.executable
    if os.path.basename(py).lower() == "pythonw.exe":   # 会议室用 pythonw 跑时，钩子用同目录的 python.exe（要读写 stdin / stdout）
        alt = os.path.join(os.path.dirname(py), "python.exe")
        py = alt if os.path.exists(alt) else py
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook_codex.py")
    # Windows 上 codex 用 PowerShell 跑钩子命令：带引号的程序路径前面要加 &（2026-10-22 实测不加就"退出码 1"）
    q = '& "%s" "%s"' if os.name == "nt" else '"%s" "%s"'
    return q % (py.replace("\\", "/"), script.replace("\\", "/"))


def codex_hook_args(exe):
    """启动参数里加的钩子 + 信任。查不到信任就不加（护栏只是少一道，不耽误干活）。"""
    hook = codex_hook_cmd()
    flag = ["-c", "hooks.PreToolUse=[{matcher=\"apply_patch\",hooks=[{type=\"command\",command='%s',timeout=15}]}]" % hook]
    with _hook_lock:
        trust = _hook_cache.get(hook)
        if trust is None:
            path = os.path.join(LOG_DIR, "codex_hook_trust.json")
            saved = read_json(path, {}) or {}
            trust = saved.get(hook)
            if not trust:
                trust = _probe_hook_trust(exe, flag, hook)
                if trust:
                    saved[hook] = trust
                    try:
                        os.makedirs(LOG_DIR, exist_ok=True)
                        from .common import write_json
                        write_json(path, saved)
                    except OSError:
                        pass
            _hook_cache[hook] = trust or ""
    if not trust:
        return []
    return flag + ["-c", "hooks.state={'%s'={trusted_hash='%s'}}" % (trust["key"], trust["hash"])]


def _probe_hook_trust(exe, flag, hook):
    srv = CodexServer(list(exe) + flag + ["app-server", "--listen", "stdio://"], {})
    try:
        srv.start()
        r = srv.request("hooks/list", {"cwds": [PROJECT]}, timeout=60)
        for d in r.get("data") or []:
            for h in d.get("hooks") or []:
                if h.get("source") == "sessionFlags" and h.get("command") == hook and h.get("currentHash") and "'" not in h.get("key", ""):
                    return {"key": h["key"], "hash": h["currentHash"]}
    except Exception:
        return None
    finally:
        srv.stop()
    return None


PATCH_FILE = re.compile(r"^\*\*\* (?:Update File|Add File|Delete File|Move to):\s*(.+?)\s*$", re.M)


def patch_paths(patch, cwd):
    """apply_patch 补丁里要动的文件（相对路径按 cwd 算），去重保序。"""
    out = []
    for m in PATCH_FILE.finditer(patch or ""):
        p = m.group(1)
        p = os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd, p))
        if p not in out:
            out.append(p)
    return out


class CodexServer:
    """一个常驻的 codex app-server（一场会议一个）。请求串行（self.call_lock），一次只跑一轮。
    每轮：thread/resume（或新建）→ turn/start → 等 turn/completed → thread/unsubscribe。
    每轮都重新接、说完就放手：用户在终端里 codex resume 聊过的，下一轮也接得上；交给终端时会议室不占着会话。"""

    def __init__(self, cmd=None, env=None):
        self.cmd, self.env = cmd, env or {}
        self.p = None
        self.q = None
        self.err = []            # stderr 最后几十行
        self.next_id = 0
        self.call_lock = threading.Lock()
        self.used = time.time()
        self.on_request = None   # 服务端反过来问的（审批之类）交给谁处理；None = 一律不批

    # --- 进程 ---
    def alive(self):
        return self.p is not None and self.p.poll() is None

    def start(self):
        cmd = self.cmd
        if cmd is None:
            cmd, env = codex_launch(None, "discuss")
            self.cmd, self.env = cmd, env
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  cwd=PROJECT, env=codex_env(self.env), **NO_WINDOW)
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
        self.request("initialize", {"clientInfo": {"name": "roundtable", "title": "AI Bridge", "version": "2"},
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
        """服务端反过来问的（审批、要用户输入等）：交给 on_request；没有就一律不批，免得一轮卡住。"""
        meth, rid = m.get("method"), m.get("id")
        if self.on_request:
            try:
                ans = self.on_request(m)
            except Exception:
                ans = None
            if ans is not None:
                self._send({"id": rid, "result": ans})
                return
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

    def ensure(self):
        if not self.alive():
            self.stop()
            self.start()
        self.used = time.time()

    # --- 一轮 ---
    def run_turn(self, sid, prompt, images, mode, model, effort, edits, res, cwd=PROJECT, name="", goal=None, on_live=None):
        """返回 (ok, 回复, 会话 id)。edits 边跑边填（崩了也留着）。goal 不是 None 时，开跑前把会话的目标同步成它。"""
        with self.call_lock:
            self.ensure()
            policy = codex_policy(mode)
            sandbox = "danger-full-access" if mode == "work" else "read-only"
            base = {"cwd": cwd, "approvalPolicy": "never", "sandbox": sandbox}
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
            if name and named.get(("codex", sid)) != name:
                try:
                    self.request("thread/name/set", {"threadId": sid, "name": name}, timeout=30)
                    named[("codex", sid)] = name
                except Exception:   # 起名失败不耽误这一轮，下一轮再试
                    pass
            if goal is not None:
                sync_codex_goal(self, sid, goal, res)
            inp = [{"type": "text", "text": prompt, "text_elements": []}]
            inp += [{"type": "localImage", "path": p} for p in images]
            tp = {"threadId": sid, "input": inp, "cwd": cwd, "approvalPolicy": "never", "sandboxPolicy": policy}
            if model:
                tp["model"] = model
            if effort:
                tp["effort"] = effort
            msgs, final, state = [], [], {"turn": None, "done": None, "error": ""}

            def live(text):
                res["live"] = text
                if on_live:
                    on_live(res)

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
                        else:
                            live(it["text"])
                    elif t == "imageGeneration":   # 自带生图：生成好的图会议室自己发出来，不靠它记得写【发送】
                        if meth == "item/started":
                            live("正在生成图片…")
                        elif it.get("savedPath"):
                            if it["savedPath"] not in res.setdefault("gen_images", []):
                                res["gen_images"].append(it["savedPath"])
                        elif it.get("failure") or it.get("status") == "failed":
                            f = it.get("failure") or {}
                            res.setdefault("gen_fail", []).append(
                                "生图额度用完了" + ("（%s 恢复）" % datetime.fromtimestamp(f["resetsAt"]).strftime("%m-%d %H:%M") if f.get("resetsAt") else "")
                                if f.get("type") == "usageLimitExceeded" else "生图失败（%s）" % (f.get("type") or it.get("status")))
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
                idle, total = timeout_for(mode)
                t0 = last = time.time()
                while state["done"] is None:
                    try:
                        m = self._next(min(last + idle, t0 + total))
                        last = time.time()
                    except TimeoutError:
                        self._interrupt(sid, state)
                        return False, timeout_text("codex", "idle" if time.time() - last >= idle else "total", mode), sid
                    if m is None:
                        return False, "codex 后台进程中途退出了\n" + "\n".join(self.err_tail()), sid
                    on_note(m)
            finally:
                if self.alive():
                    try:
                        self.request("thread/unsubscribe", {"threadId": sid}, timeout=30)
                    except Exception:
                        pass
                self.used = time.time()
            turn = state["done"]
            status = turn.get("status")
            if status != "completed":
                msg = ((turn.get("error") or {}).get("message")) or state["error"] or status
                return False, "codex 这一轮没跑完（%s）" % msg, sid
            out = "\n\n".join(final) if final else (msgs[-1] if msgs else "")
            return True, out, sid

    def compact(self, sid, cwd=PROJECT, res=None):
        """手动压缩一个会话：thread/resume → thread/compact/start → 等这一轮 turn/completed → thread/unsubscribe。
        返回 True = 看到了压缩（contextCompaction 条目或 thread/compacted 通知）。"""
        with self.call_lock:
            self.ensure()
            base = {"cwd": cwd, "approvalPolicy": "never", "sandbox": "read-only"}
            self.request("thread/resume", dict(base, threadId=sid, excludeTurns=True), timeout=180)
            seen = {"compacted": False, "done": False}

            def on_note(m):
                meth, pr = m.get("method"), m.get("params") or {}
                if pr.get("threadId") not in (None, sid):
                    return
                if meth == "thread/compacted":
                    seen["compacted"] = True
                elif meth == "item/completed" and (pr.get("item") or {}).get("type") == "contextCompaction":
                    seen["compacted"] = True
                elif meth == "thread/tokenUsage/updated" and res is not None:
                    tu = pr.get("tokenUsage") or {}
                    last = tu.get("last") or {}
                    if last.get("totalTokens"):
                        res["ctx_used"] = last["totalTokens"]
                    if tu.get("modelContextWindow"):
                        res["ctx_win"] = tu["modelContextWindow"]
                elif meth == "turn/completed":
                    seen["done"] = True

            try:
                self.request("thread/compact/start", {"threadId": sid}, timeout=120, on_note=on_note)
                deadline = time.time() + 600
                while not seen["done"]:
                    m = self._next(deadline)
                    if m is None:
                        raise RuntimeError("codex 后台进程中途退出了")
                    on_note(m)
            finally:
                if self.alive():
                    try:
                        self.request("thread/unsubscribe", {"threadId": sid}, timeout=30)
                    except Exception:
                        pass
                self.used = time.time()
            return seen["compacted"]

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


_codex_lock = threading.Lock()


def codex_for(room, mode):
    """这场会议的 app-server；配置（模式、工具）变了就换一个新的。"""
    cmd, env = codex_launch(room, mode)
    with _codex_lock:
        srv = room.codex if room is not None else None
        if srv is not None and (srv.cmd != cmd or srv.env != env):
            srv.stop()
            srv = None
        if srv is None:
            srv = CodexServer(cmd, env)
            srv.on_request = (lambda m, r=room: codex_server_request(r, m)) if room is not None else None
            if room is not None:
                room.codex = srv
        return srv


def stop_codex(room):
    """停掉这场会议的 app-server（"停"、换配置、会议删了）；下一轮自动重起，会话从记录里接上。"""
    if room is None:
        return
    with _codex_lock:
        srv, room.codex = room.codex, None
    if srv is not None:
        srv.stop()


def start_reaper():
    """后台每分钟看一眼：闲了很久的 app-server 关掉。"""
    def loop():
        from . import room as R
        while True:
            time.sleep(60)
            for r in R.rooms.all():
                srv = r.codex
                if srv is not None and srv.alive() and not r.busy_for("codex") and not srv.call_lock.locked() \
                        and time.time() - srv.used > CODEX_IDLE_STOP:
                    stop_codex(r)
    threading.Thread(target=loop, daemon=True, name="codex-reaper").start()


def call_codex_appserver(room, prompt, sid, images, mode, model, effort, edits, res):
    try:
        srv = codex_for(room, mode)
        goal = room.goal() if room is not None else None
        on_live = (lambda rr: room.push_live("codex", rr)) if room is not None else None
        ok, out, new_sid = srv.run_turn(sid, prompt, list(images), mode, model, effort, edits, res,
                                        cwd=room.workdir() if room is not None else PROJECT,
                                        name=room.session_name() if room is not None else "",
                                        goal=goal, on_live=on_live)
    except Exception as e:
        srv = room.codex if room is not None else None
        tail = "\n".join(srv.err_tail()) if srv is not None else ""
        stop_codex(room)
        return False, "codex 调用失败：%s%s" % (e, ("\n" + tail) if tail else ""), sid
    if not ok:
        return False, out, sid
    # 自带生图生成的图：按【发送】挂到这条回复上（它自己也写了同一张的，apply_send_cmds 会去重）
    extra = ["【发送】%s" % p for p in res.get("gen_images") or []]
    extra += ["（%s）" % x for x in res.get("gen_fail") or []]
    if extra:
        out = (out + "\n\n" + "\n".join(extra)).strip()
    if not out:
        return False, "codex 没有返回内容", sid
    return True, out, new_sid


def sync_codex_goal(srv, sid, goal, res):
    """把会议目标同步成 codex 会话的目标（暂不启用，见 README）。"""
    return None


def codex_server_request(room, m):
    """codex 反过来问的：现在只认文件改动的审批（文件护栏），别的照旧不批。"""
    return None


def delete_codex_thread(sid):
    """经一个临时的 app-server 删 codex 的会话（连它的记录文件）。返回 (ok, 说明)。"""
    from .common import SID_RE
    if not SID_RE.match(sid or ""):
        return False, "id 不对"
    srv = CodexServer()
    try:
        srv.start()
        srv.request("thread/delete", {"threadId": sid}, timeout=60)
        return True, ""
    except Exception as e:
        msg = str(e)
        if "not found" in msg.lower() or "no thread" in msg.lower() or "找不到" in msg:
            return True, "本来就没有"
        return False, msg
    finally:
        srv.stop()


# ===================== 文件护栏：钩子来问 =====================
def guard_allow(kind):
    return {}   # 空 = 不表态，照常放行


def guard_decide(kind, room, ai, data):
    """钩子带来的工具调用：要改哪些文件 → 问 guard。返回给钩子的回应。"""
    if room is None:
        return guard_allow(kind)
    if kind == "claude":
        inp = data.get("tool_input") or {}
        p = inp.get("file_path") or inp.get("notebook_path")
        if not p:
            return {}
        p = p if os.path.isabs(p) else os.path.join(data.get("cwd") or room.workdir(), p)
        ok, hit = guard.touch((room.id, ai), p, room.title())
        if ok:
            return {}
        room.add_message("系统", block_note(ai, p, hit), not_for=ai)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": block_text(p, hit)}}
    if kind == "codex":
        inp = data.get("tool_input") or {}
        patch = inp.get("command") if isinstance(inp, dict) else inp
        if isinstance(patch, list):
            patch = "\n".join(str(x) for x in patch)
        paths = patch_paths(patch if isinstance(patch, str) else "", data.get("cwd") or room.workdir())
        for p in paths:
            ok, hit = guard.touch((room.id, ai), p, room.title())
            if not ok:
                room.add_message("系统", block_note(ai, p, hit), not_for=ai)
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": block_text(p, hit)}}
        return {}
    return guard_allow(kind)
