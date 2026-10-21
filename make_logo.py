from PIL import Image, ImageDraw, ImageFont, ImageFilter
import math

S = 1024
P_DARK = (38, 33, 92)
P_MID = (83, 74, 183)
P_LIGHT = (127, 119, 221)
WHITE = (255, 255, 255)
GOLD = (250, 199, 117)

F = "/usr/share/fonts/opentype/inter/"
f_know = ImageFont.truetype(F + "InterDisplay-SemiBold.otf", 118)
f_city = ImageFont.truetype(F + "InterDisplay-Black.otf", 212)
f_we = ImageFont.truetype(F + "InterDisplay-Black.otf", 50)
f_by = ImageFont.truetype(F + "Inter-SemiBold.otf", 32)


def gradient_bg():
    img = Image.new("RGB", (S, S), P_MID)
    px = img.load()
    for y in range(S):
        for x in range(S):
            t = (x + y) / (2 * S)
            px[x, y] = tuple(int(P_LIGHT[i] * (1 - t) + P_DARK[i] * t) for i in range(3))
    return img


def skyline(d, base_y):
    """Simple city skyline silhouette across the bottom, semi transparent white."""
    bld = [(70, 200), (130, 150), (200, 260), (260, 180), (330, 300),
           (400, 220), (470, 340), (540, 250), (610, 300), (680, 190), (750, 280),
           (820, 160), (890, 230), (960, 140), (1024, 180)]
    col = (255, 255, 255, 28)
    x0 = 0
    for x1, h in bld:
        d.rectangle((x0, base_y - h, x1 - 6, base_y), fill=col)
        # windows
        for wy in range(base_y - h + 18, base_y - 10, 34):
            for wx in range(x0 + 12, x1 - 18, 26):
                d.rectangle((wx, wy, wx + 9, wy + 13), fill=(255, 255, 255, 40))
        x0 = x1


def pin(d, cx, cy, r):
    # glow
    d.ellipse((cx - r - 30, cy - r - 30, cx + r + 30, cy + r + 30), fill=(255, 255, 255, 22))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=WHITE)
    d.polygon([(cx - r * 0.72, cy + r * 0.69), (cx + r * 0.72, cy + r * 0.69), (cx, cy + r * 2.0)], fill=WHITE)
    d.ellipse((cx - r * 0.52, cy - r * 0.52, cx + r * 0.52, cy + r * 0.52), fill=P_MID)
    # question mark inside the pin
    fq = ImageFont.truetype(F + "InterDisplay-Black.otf", int(r * 0.78))
    w = d.textlength("?", font=fq)
    d.text((cx - w / 2, cy - r * 0.50), "?", font=fq, fill=WHITE)


def center_text(d, y, text, font, fill, spacing=0):
    if spacing:
        total = sum(d.textlength(c, font=font) for c in text) + spacing * (len(text) - 1)
        x = (S - total) / 2
        for c in text:
            d.text((x, y), c, font=font, fill=fill)
            x += d.textlength(c, font=font) + spacing
    else:
        w = d.textlength(text, font=font)
        d.text(((S - w) / 2, y), text, font=font, fill=fill)


def build(circle: bool, path: str):
    img = gradient_bg().convert("RGBA")
    ov = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    skyline(d, 1024)
    pin(d, S // 2, 215, 86)
    img = Image.alpha_composite(img, ov)
    d = ImageDraw.Draw(img)

    center_text(d, 420, "KNOW YOUR", f_know, WHITE, spacing=4)
    # subtle shadow under CITY
    center_text(d, 540 + 6, "CITY", f_city, (0, 0, 0, 60), spacing=6)
    center_text(d, 540, "CITY", f_city, WHITE, spacing=6)
    # gold underline
    d.rounded_rectangle((S // 2 - 150, 790, S // 2 + 150, 802), radius=6, fill=GOLD)

    # WE badge
    bw = 96
    bx, by = (S - bw) // 2, 838
    d.ellipse((bx - 4, by - 4, bx + bw + 4, by + bw + 4), fill=GOLD)
    d.ellipse((bx, by, bx + bw, by + bw), fill=WHITE)
    w = d.textlength("WE", font=f_we)
    d.text(((S - w) / 2, by + 22), "WE", font=f_we, fill=P_MID)

    if circle:
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, S, S), fill=255)
        out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        out.save(path)
    else:
        center_text(d, 962, "BY WORSHIP EARTH FOUNDATION", f_by, (230, 228, 250), spacing=3)
        img.convert("RGB").save(path)


build(False, "/home/claude/knowyourcity/logo_square.png")
build(True, "/home/claude/knowyourcity/logo_circle.png")
print("ok")
