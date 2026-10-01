"""시연용 합성 증빙 이미지 생성 (실제 현장 사진이 아님 — LIMITATIONS.md L-D05).

- lock_and_tag.png : 차단기 손잡이에 자물쇠 + 꼬리표 (M-02b 충족 예시)
- tag_only.png     : 꼬리표만, 자물쇠 없음 (M-02b 불충족 예시)
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "samples"


def font(size):
    for name in ("malgun.ttf", "NanumGothic.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def panel(draw):
    draw.rectangle([80, 60, 560, 620], fill=(150, 155, 160), outline=(60, 60, 60), width=6)       # 분전반
    draw.rectangle([230, 180, 410, 420], fill=(40, 40, 45), outline=(20, 20, 20), width=4)        # 차단기 본체
    draw.rectangle([300, 200, 340, 330], fill=(200, 30, 30), outline=(90, 0, 0), width=3)         # 손잡이(OFF 위치)
    draw.text((255, 430), "MAIN BREAKER  OFF", fill=(20, 20, 20), font=font(22))
    draw.text((110, 80), "P-03 주전원", fill=(20, 20, 20), font=font(30))


def tag(draw, x, y):
    draw.line([(x, y), (x, y + 60)], fill=(30, 30, 30), width=3)
    draw.rectangle([x - 70, y + 60, x + 70, y + 230], fill=(230, 40, 30), outline=(120, 0, 0), width=3)
    f = font(22)
    draw.text((x - 55, y + 75), "위험", fill="white", font=font(34))
    draw.text((x - 60, y + 125), "조작금지", fill="white", font=f)
    draw.text((x - 60, y + 160), "DO NOT", fill="white", font=f)
    draw.text((x - 60, y + 190), "김정비", fill="white", font=f)


def padlock(draw, x, y):
    draw.arc([x - 32, y - 60, x + 32, y + 10], start=180, end=360, fill=(170, 170, 175), width=12)   # 걸쇠
    draw.line([(x - 32, y - 25), (x - 32, y + 5)], fill=(170, 170, 175), width=12)
    draw.line([(x + 32, y - 25), (x + 32, y + 5)], fill=(170, 170, 175), width=12)
    draw.rounded_rectangle([x - 50, y, x + 50, y + 95], radius=12, fill=(240, 190, 20), outline=(120, 90, 0), width=4)
    draw.ellipse([x - 9, y + 30, x + 9, y + 48], fill=(40, 40, 40))
    draw.rectangle([x - 4, y + 45, x + 4, y + 70], fill=(40, 40, 40))


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    a = Image.new("RGB", (640, 700), (225, 225, 220))
    d = ImageDraw.Draw(a)
    panel(d)
    padlock(d, 320, 330)
    tag(d, 320, 425)
    a.save(OUT / "lock_and_tag.png")

    b = Image.new("RGB", (640, 700), (225, 225, 220))
    d = ImageDraw.Draw(b)
    panel(d)
    tag(d, 320, 330)
    b.save(OUT / "tag_only.png")
    return OUT


if __name__ == "__main__":
    print(build())
