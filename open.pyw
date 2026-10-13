"""桌面快捷方式的入口：会议室没开就在后台开起来，然后用浏览器打开。不弹黑窗口。"""
import os, subprocess, sys, time, urllib.request, webbrowser

URL = "http://127.0.0.1:8765/"
HERE = os.path.dirname(os.path.abspath(__file__))


def alive():
    try:
        urllib.request.urlopen(URL, timeout=2)
        return True
    except Exception:
        return False


if not alive():
    env = dict(os.environ, ROUNDTABLE_NO_BROWSER="1", PYTHONIOENCODING="utf-8")
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(exe):
        exe = sys.executable
    subprocess.Popen([exe, os.path.join(HERE, "roundtable.py")], cwd=HERE, env=env,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                     | subprocess.CREATE_NO_WINDOW, close_fds=True)
    for _ in range(40):
        time.sleep(0.25)
        if alive():
            break

webbrowser.open(URL)
