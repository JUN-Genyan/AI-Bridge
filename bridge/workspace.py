# -*- coding: utf-8 -*-
"""干活模式：存档点、改动清单、撤回要用到的 git 小工具。都按传进来的工作文件夹干活，不认"当前会议"。"""
import hashlib, os, re, shutil, subprocess, threading

from .common import LOG_DIR, NO_WINDOW, in_dir, norm

snap_lock = threading.Lock()   # 几个会议室同一个仓库拍快照时共用一份私有 index，得排队
GIT_C = ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.quotepath=false",
         "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false"]
GIT_IDENT = {"GIT_AUTHOR_NAME": "roundtable", "GIT_AUTHOR_EMAIL": "roundtable@localhost",
             "GIT_COMMITTER_NAME": "roundtable", "GIT_COMMITTER_EMAIL": "roundtable@localhost"}
ZERO_SHA = "0" * 40
STATUS_CN = {"M": "改", "A": "增", "D": "删", "T": "改"}
MAX_LIST = 40
# 会议室自己天天在写的东西（在会议记录目录里），不进快照
OWN_NAMES = ("meetings", "trash", "current.txt", "claims.json", "bridge_token.txt")


def git(args, cwd, env_extra=None, check=True):
    env = dict(os.environ, **GIT_IDENT)
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(["git"] + GIT_C + list(args), cwd=cwd, env=env, capture_output=True, timeout=600, **NO_WINDOW)
    if check and r.returncode != 0:
        raise RuntimeError("git %s 失败：%s" % (args[0], r.stderr.decode("utf-8", "replace").strip()[:300]))
    return r.stdout if check else r


def git_dirs(cwd):
    out = git(["rev-parse", "--show-toplevel", "--absolute-git-dir"], cwd).decode("utf-8", "replace").splitlines()
    return os.path.normpath(out[0]), os.path.normpath(out[1])


def snapshot_index(top):
    """私有 index 每个仓库一份（换了工作文件夹不能混用）。"""
    key = os.path.normcase(os.path.realpath(top))
    return os.path.join(LOG_DIR, "snapshot-%s.index" % hashlib.sha1(key.encode("utf-8")).hexdigest()[:10])


def in_git(cwd):
    try:
        return git(["rev-parse", "--show-toplevel"], cwd, check=False).returncode == 0
    except Exception:
        return False


def snapshot_tree(cwd):
    """不碰用户的 index 和当前分支，把工作区现状（不含 .gitignore 忽略的、不含会议室自己的目录）拍成一棵 tree，返回 tree id。
    用一份私有 index 文件（首次从用户 index 复制，之后沿用，保住文件状态缓存，不用每次重算大文件）。"""
    with snap_lock:
        top, gd = git_dirs(cwd)
        idx = snapshot_index(top)
        os.makedirs(LOG_DIR, exist_ok=True)
        env = {"GIT_INDEX_FILE": idx}
        spec = [":/"]
        # 会议室目录不在这个仓库里（工作文件夹换到别的盘时 relpath 会直接抛 ValueError）：不用排除，也不用算相对路径
        inside = in_dir(LOG_DIR, top)
        rel = os.path.relpath(os.path.realpath(LOG_DIR), os.path.realpath(top)).replace("\\", "/") if inside else ""
        # 会议室目录已经被 .gitignore 忽略时不用再排除；点名排除被忽略的路径，git add 会报错退出
        ignored = inside and git(["check-ignore", "-q", rel + "/"], cwd, check=False).returncode == 0
        if inside and not ignored:
            # 只排除会议室自己天天在写的：会议记录目录、current.txt、私有 index 之类；这个目录里别的文件照常算
            for x in OWN_NAMES:
                spec.append(":(exclude,top)%s/%s" % (rel, x))
            spec.append(":(exclude,top,glob)%s/snapshot*.index*" % rel)
        for attempt in (0, 1):
            if not os.path.exists(idx):
                real = os.path.join(gd, "index")
                if os.path.exists(real):
                    shutil.copyfile(real, idx)
                elif git(["rev-parse", "--verify", "-q", "HEAD"], cwd, check=False).returncode == 0:
                    git(["read-tree", "HEAD"], cwd, env)
            try:
                git(["add", "-A", "--"] + spec, cwd, env)
                break
            except RuntimeError:
                if attempt:
                    raise
                try:
                    os.remove(idx)
                except OSError:
                    pass
        return git(["write-tree"], cwd, env).decode().strip()


def save_checkpoint(cwd, ref_name, seq, pre_tree):
    """把"改前"的树包成一个提交，挂在 refs/roundtable/<会议>/<序号> 上，防止被 gc（撤回要从这里取原文件）。"""
    head = git(["rev-parse", "--verify", "-q", "HEAD"], cwd, check=False)
    parent = head.stdout.decode().strip() if head.returncode == 0 else None
    base = "refs/roundtable/%s/%d" % (ref_name, seq)
    c0 = git(["commit-tree", pre_tree] + (["-p", parent] if parent else []) + ["-m", "roundtable checkpoint"], cwd).decode().strip()
    git(["update-ref", base, c0], cwd)
    return base


def purge_refs(ref_name, cwd):
    """清掉 refs/roundtable/<ref_name>/* 的存档点引用。返回删掉的个数。"""
    r = git(["for-each-ref", "--format=%(refname)", "refs/roundtable/%s/" % ref_name], cwd, check=False)
    n = 0
    if r.returncode == 0:
        for ref in r.stdout.decode("utf-8", "replace").split():
            if git(["update-ref", "-d", ref], cwd, check=False).returncode == 0:
                n += 1
    return n


def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


def tree_blobs(cwd, tree, rels):
    """某棵树里这些路径各自的 blob id（树里没有 = None）。一次 git cat-file --batch-check 搞定。"""
    if not rels:
        return {}
    inp = "".join("%s:%s\n" % (tree, r) for r in rels).encode("utf-8")
    r = subprocess.run(["git"] + GIT_C + ["cat-file", "--batch-check"], cwd=cwd, input=inp, capture_output=True,
                       env=dict(os.environ, **GIT_IDENT), timeout=120, **NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("git cat-file 失败：" + r.stderr.decode("utf-8", "replace").strip()[:300])
    lines = r.stdout.decode("utf-8", "replace").splitlines()
    out = {}
    for rel, ln in zip(rels, lines):
        out[rel] = ln.split()[0] if re.match(r"^[0-9a-f]{40} blob \d+$", ln.strip()) else None
    return out


def snap_excluded(full):
    """会议室自己的目录里不进快照的那些（meetings / current.txt / 私有 index 之类）：改了也无存档可还原。"""
    if not in_dir(full, LOG_DIR):
        return False
    first = os.path.relpath(os.path.realpath(full), os.path.realpath(LOG_DIR)).split(os.sep)[0]
    return first in OWN_NAMES or first.startswith("snapshot")


def is_ignored(cwd, full):
    return git(["check-ignore", "-q", "--", full], cwd, check=False).returncode == 0


def split_edits(edits, top, wd):
    """把 AI 的操作记录里的路径规范成相对仓库根的正斜杠路径。返回 ([(rel, full)], [项目外的绝对路径])，各自去重保序。"""
    inside, outside, seen = [], [], set()
    for e in edits or []:
        p = e.get("path")
        if not p:
            continue
        full = os.path.normpath(p if os.path.isabs(p) else os.path.join(wd, p))
        key = os.path.normcase(full)
        if key in seen:
            continue
        seen.add(key)
        if in_dir(full, top):
            inside.append((os.path.relpath(full, top).replace("\\", "/"), full))
        else:
            outside.append(full)
    return inside, outside


def build_files(cwd, pre_tree, inside):
    """对 AI 记录里的每个文件：读"改前"（pre_tree 里的 blob）和"改后"（现在磁盘上的字节，存 sha256）。没变化的不算。"""
    blobs = tree_blobs(cwd, pre_tree, [r for r, _ in inside])
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
            nb = snap_excluded(full) or is_ignored(cwd, full)
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


def full_of(top, rel):
    full = os.path.normpath(os.path.join(top, rel))
    if not in_dir(full, top) or in_dir(full, LOG_DIR):
        raise ValueError("路径不安全：" + rel)
    return full


def remove_empty_dirs(full, top):
    d = os.path.dirname(full)
    while in_dir(d, top) and norm(d) != norm(top):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)
