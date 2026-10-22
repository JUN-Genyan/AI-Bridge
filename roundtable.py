# -*- coding: utf-8 -*-
"""AI Bridge 会议室的启动入口（桌面快捷方式 open.pyw、start.bat 都跑它）。代码在 bridge 包里，见 README 的"代码结构"。"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge.web import main

if __name__ == "__main__":
    main()
