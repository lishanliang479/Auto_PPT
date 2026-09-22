from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
draw = ImageDraw.Draw(canvas)

# 使用简单的蓝色圆角图标，避免依赖目标电脑字体或外部素材。
for offset in range(112, -1, -1):
    ratio = offset / 112
    color = (35, int(105 + 45 * ratio), int(205 + 30 * ratio), 255)
    draw.rounded_rectangle((16 + offset // 12, 16 + offset // 12,
                            240 - offset // 12, 240 - offset // 12),
                           radius=54, fill=color)

font_path = Path(r"C:\Windows\Fonts\arialbd.ttf")
font = ImageFont.truetype(str(font_path), 142) if font_path.exists() else ImageFont.load_default()
text = "P"
box = draw.textbbox((0, 0), text, font=font)
x = (256 - (box[2] - box[0])) / 2
y = (256 - (box[3] - box[1])) / 2 - box[1] - 7
draw.text((x, y), text, font=font, fill="white")
canvas.save(ROOT / "autoppt.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
