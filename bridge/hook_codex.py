# -*- coding: utf-8 -*-
"""codex 的 PreToolUse 钩子（文件护栏）：codex 要用 apply_patch 改文件之前跑这个脚本。
从 stdin 读钩子的 JSON（tool_input.command 是补丁全文），交给会议室判断；会议室说不行就输出拒绝，codex 不改。
会议室没开、出错、超时：什么都不输出，照常放行（护栏是防撞的，不该把 AI 卡死）。只用标准库。"""
import json, os, sys, urllib.request


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except Exception:
        return
    dbg = os.environ.get("AIBRIDGE_HOOK_LOG")   # 调试：把收到的记下来
    if dbg:
        with open(dbg, "a", encoding="utf-8") as f:
            f.write(json.dumps({"stdin": data, "has_key": bool(os.environ.get("AIBRIDGE_KEY"))}, ensure_ascii=False) + "\n")
    url, key = os.environ.get("AIBRIDGE_URL"), os.environ.get("AIBRIDGE_KEY")
    if not url or not key:
        return
    req = urllib.request.Request(url.rstrip("/") + "/api/guard/codex", method="POST",
                                 data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            out = json.loads(r.read().decode("utf-8") or "{}")
    except Exception:
        return
    if (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny":
        sys.stdout.write(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
