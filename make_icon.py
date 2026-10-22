"""生成会议室图标：俯视的一张圆桌，三个座位（用户 / claude / codex）。
输出 roundtable.ico（桌面快捷方式）、roundtable_preview.png，以及装成 app 用的 static/icon-192.png、icon-512.png、icon-maskable-512.png。"""
import math, os
from PIL import Image, ImageDraw

S = 1024  # 先画大图再缩小，边缘更平滑
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 底板：深墨蓝圆角方块
d.rounded_rectangle([40, 40, S - 40, S - 40], radius=220, fill=(28, 32, 48, 255))

c = S / 2
# 桌面：暖灰圆环
R = 250
d.ellipse([c - R, c - R, c + R, c + R], fill=(58, 64, 86, 255))
r2 = 150
d.ellipse([c - r2, c - r2, c + r2, c + r2], fill=(28, 32, 48, 255))

# 三个座位，分别是用户（白）、claude（橙）、codex（青）
seats = [(-90, (240, 240, 235)), (30, (224, 122, 72)), (150, (72, 196, 178))]
sr = 120
for ang, col in seats:
    a = math.radians(ang)
    x, y = c + 330 * math.cos(a), c + 330 * math.sin(a)
    d.ellipse([x - sr, y - sr, x + sr, y + sr], fill=col + (255,))
    # 每个座位朝桌心伸一道细线，像在对话
    x2, y2 = c + 170 * math.cos(a), c + 170 * math.sin(a)
    d.line([x, y, x2, y2], fill=col + (255,), width=34)

# 桌心一点，代表公告
d.ellipse([c - 46, c - 46, c + 46, c + 46], fill=(240, 240, 235, 255))

here = os.path.dirname(os.path.abspath(__file__))
out = os.path.join(here, "roundtable.ico")
img.resize((256, 256), Image.LANCZOS).save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.resize((256, 256), Image.LANCZOS).save(out.replace(".ico", "_preview.png"))
print(out)

# 装成 app 用的图标：普通的就是上面这张；maskable 的底色铺满整张、图案缩进安全区（手机会把它裁成圆形 / 圆角方块）
st = os.path.join(here, "static")
os.makedirs(st, exist_ok=True)
for n in (192, 512):
    img.resize((n, n), Image.LANCZOS).save(os.path.join(st, "icon-%d.png" % n))
mask = Image.new("RGBA", (S, S), (28, 32, 48, 255))
inner = img.resize((int(S * 0.8), int(S * 0.8)), Image.LANCZOS)
mask.alpha_composite(inner, (int(S * 0.1), int(S * 0.1)))
mask.resize((512, 512), Image.LANCZOS).save(os.path.join(st, "icon-maskable-512.png"))
print(st)
