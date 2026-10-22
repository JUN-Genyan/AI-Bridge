# -*- coding: utf-8 -*-
"""AI Bridge：本机的多会议室服务（用户 + Claude Code + Codex）。只用标准库，只绑 127.0.0.1。

模块：
- common     路径、上限、规矩文本、小工具
- workspace  干活模式的存档点、改动清单、撤回（git）
- guard      文件护栏：谁占着哪个文件，跨会议室
- agents     怎么叫 claude（每轮一个 claude -p）和 codex（每个会议室一个 codex app-server）
- room       一场会议 = 一个 Room：消息、排队、叫 AI、看板、压缩、停……；会议目录的增删
- mcp        会议室自己的 MCP 服务（给会议里的 AI 和外面的 AI 用）
- web        HTTP 接口和启动
"""
