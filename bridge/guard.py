# -*- coding: utf-8 -*-
"""文件护栏：同一个文件同一时间只让一个 AI 改，跨会议室。

两种占法：
- 自动（auto）：AI 用编辑工具第一次改某个文件时（claude 的 PreToolUse 钩子、codex 的审批 / 钩子会先来问），
  会议室替它占住，这一轮说完放开；一轮里 30 分钟没再碰也放开（防止一轮跑两个钟头把早改过的文件一直锁着）。
- 明着占（claim）：AI 用 bridge 工具 claim_files 占一批文件或文件夹，写原因和多久（默认 30 分钟，最多 4 小时），
  做完 release_files；用户在页面上也能放开任何一个。明着占的存进 claims.json，重启还在。

同一个"人"（同一场会议的同一个 AI）自己占的不挡自己。用户自己占（"你"）的谁都挡。
这只管编辑工具：命令行写文件挡不住（规矩里说了不许绕），撤回清单也不记。"""
import os, threading, time

from .common import LOG_DIR, norm, read_json, write_json, now_str

AUTO_IDLE = 30 * 60
CLAIM_DEFAULT = 30
CLAIM_MAX = 240
USER = "你"


def who_text(it):
    if it["ai"] == USER:
        return "你（用户）"
    return "%s（会议「%s」）" % (it["ai"], it.get("room_title") or it["room"])


class FileGuard:
    def __init__(self, path=None):
        self.lock = threading.Lock()
        self.path = path or os.path.join(LOG_DIR, "claims.json")
        self.items = {}      # 规范化路径 -> {key, shown, dir, room, room_title, ai, kind, since, last, until, reason}
        self._load()

    # --- 存取 ---
    def _load(self):
        data = read_json(self.path, []) or []
        now = time.time()
        for it in data if isinstance(data, list) else []:
            if isinstance(it, dict) and it.get("kind") == "claim" and it.get("key") and (it.get("until") or 0) > now:
                self.items[it["key"]] = it

    def _save(self):
        keep = [it for it in self.items.values() if it["kind"] == "claim"]
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            write_json(self.path, keep)
        except OSError:
            pass

    @staticmethod
    def key_of(path):
        return norm(os.path.abspath(path))

    def _expire(self, now):
        dead = [k for k, it in self.items.items()
                if (it["kind"] == "claim" and it["until"] <= now) or (it["kind"] == "auto" and now - it["last"] > AUTO_IDLE)]
        for k in dead:
            del self.items[k]
        if dead:
            self._save()

    def _conflict(self, key, is_dir, who):
        """别人占着的、跟这个路径重叠的（同一个文件，或者有一方是包含另一方的文件夹）。"""
        for it in self.items.values():
            if (it["room"], it["ai"]) == who:
                continue
            k = it["key"]
            if k == key:
                return it
            if it.get("dir") and key.startswith(k.rstrip(os.sep) + os.sep):
                return it
            if is_dir and k.startswith(key.rstrip(os.sep) + os.sep):
                return it
        return None

    # --- 用 ---
    def touch(self, who, path, room_title=""):
        """编辑工具要改 path 之前来问。能改就（续）占上并返回 (True, None)；别人占着返回 (False, 占着的那条)。"""
        key = self.key_of(path)
        now = time.time()
        with self.lock:
            self._expire(now)
            hit = self._conflict(key, False, who)
            if hit:
                return False, dict(hit)
            it = self.items.get(key)
            if it and (it["room"], it["ai"]) == who:
                it["last"] = now   # 自己明着占的也算碰过；自动的续上
                return True, None
            self.items[key] = {"key": key, "shown": os.path.normpath(path), "dir": False, "room": who[0], "room_title": room_title,
                               "ai": who[1], "kind": "auto", "since": now, "last": now, "until": None, "reason": "",
                               "time": now_str()}
            return True, None

    def claim(self, who, paths, minutes=None, reason="", room_title=""):
        """明着占一批路径（文件或文件夹）。返回 [(路径, ok, 挡住它的那条或 None)]。"""
        try:
            minutes = int(minutes or CLAIM_DEFAULT)
        except (TypeError, ValueError):
            minutes = CLAIM_DEFAULT
        minutes = max(1, min(CLAIM_MAX, minutes))
        now = time.time()
        out = []
        with self.lock:
            self._expire(now)
            for p in paths:
                key = self.key_of(p)
                is_dir = os.path.isdir(p) or str(p).endswith(("/", "\\"))
                hit = self._conflict(key, is_dir, who)
                if hit:
                    out.append((p, False, dict(hit)))
                    continue
                self.items[key] = {"key": key, "shown": os.path.normpath(p), "dir": is_dir, "room": who[0], "room_title": room_title,
                                   "ai": who[1], "kind": "claim", "since": now, "last": now, "until": now + minutes * 60,
                                   "reason": (reason or "")[:200], "time": now_str()}
                out.append((p, True, None))
            self._save()
        return out

    def release(self, who, paths=None):
        """放开自己占的（paths 为空 = 全部，自动的和明着占的都放）。返回放开的个数。"""
        keys = None if not paths else {self.key_of(p) for p in paths}
        with self.lock:
            dead = [k for k, it in self.items.items() if (it["room"], it["ai"]) == who and (keys is None or k in keys)]
            for k in dead:
                del self.items[k]
            self._save()
        return len(dead)

    def release_turn(self, who):
        """一轮说完：放开这一轮自动占的（明着占的留着）。"""
        with self.lock:
            dead = [k for k, it in self.items.items() if (it["room"], it["ai"]) == who and it["kind"] == "auto"]
            for k in dead:
                del self.items[k]
        return len(dead)

    def release_room(self, room):
        """会议删了：它占的全放开。"""
        with self.lock:
            dead = [k for k, it in self.items.items() if it["room"] == room]
            for k in dead:
                del self.items[k]
            self._save()
        return len(dead)

    def force_release(self, key):
        """用户在页面上点"放开"。返回被放开的那条（没有返回 None）。"""
        with self.lock:
            it = self.items.pop(key, None)
            self._save()
        return it

    def listing(self, room=None):
        now = time.time()
        with self.lock:
            self._expire(now)
            out = []
            for it in sorted(self.items.values(), key=lambda x: x["since"]):
                if room is not None and it["room"] != room:
                    continue
                d = {k: it[k] for k in ("key", "shown", "dir", "room", "room_title", "ai", "kind", "reason", "time")}
                d["who"] = who_text(it)
                d["mins_left"] = int((it["until"] - now) // 60) if it["kind"] == "claim" else None
                out.append(d)
            return out


def block_text(path, hit):
    """挡下时给 AI 看的话。"""
    how = "这一轮自动占着" if hit["kind"] == "auto" else "明着占着（%s%s）" % (
        "还有约 %d 分钟" % max(1, int((hit["until"] - time.time()) // 60)), "，原因：" + hit["reason"] if hit.get("reason") else "")
    if hit["ai"] == USER:
        return "文件护栏：%s 被用户占着（%s），别改它；要改先在回复里问用户。" % (path, hit.get("reason") or "用户自己在改")
    return ("文件护栏：%s 正被 %s %s，你现在改不了。先做别的，或者在回复里跟它 / 用户商量；不要换成命令行去写它。"
            % (path, who_text(hit), how))


def block_note(ai, path, hit):
    """挡下时插进会议的一句系统消息（给人看）。"""
    how = "这一轮自动占着" if hit["kind"] == "auto" else "明着占着" + ("（%s）" % hit["reason"] if hit.get("reason") else "")
    return "文件护栏挡下了 %s：它想改 %s，这个文件正被 %s %s" % (ai, path, who_text(hit), how)


guard = FileGuard()
