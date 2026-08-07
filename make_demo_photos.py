# -*- coding: utf-8 -*-
r"""
make_demo_photos.py — 시연용 '그럴싸한' 사진 생성기.

⚠️ 실사진이 아니다. 천 바탕(색 + 결 무늬) 위에 얼룩(불규칙한 반점)을 그려낸 이미지다.
왜 그리나: 예시 티켓에 사진이 붙어 있어야 시연이 실감 나는데, 실제 얼룩 사진을 지어낼 수는
없으니 "사진이 붙는 자리"를 데모 이미지로 채우는 것. 진짜 사진이 생기면 갈아끼우면 된다.

실행:  .venv\Scripts\python make_demo_photos.py   → uploads\demo_*.png 다섯 장 생성
"""

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).parent / "uploads"
OUT.mkdir(exist_ok=True)
random.seed(42)          # 돌릴 때마다 같은 그림이 나오게 (재현 가능)


def fabric(w: int, h: int, base: tuple) -> Image.Image:
    """천 바탕: 바탕색 위에 가로세로 옅은 줄을 그어 직물 결처럼 보이게."""
    img = Image.new("RGB", (w, h), base)
    d = ImageDraw.Draw(img)
    for y in range(0, h, 4):
        d.line([(0, y), (w, y)], fill=tuple(min(255, c + 6) for c in base), width=1)
    for x in range(0, w, 4):
        d.line([(x, 0), (x, h)], fill=tuple(max(0, c - 4) for c in base), width=1)
    return img


def blob(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color: tuple, n: int = 9):
    """얼룩: 원 여러 개를 어긋나게 겹쳐 불규칙한 반점 모양을 만든다."""
    for _ in range(n):
        dx, dy = random.randint(-r // 2, r // 2), random.randint(-r // 2, r // 2)
        rr = random.randint(r // 2, r)
        d.ellipse([cx + dx - rr, cy + dy - rr, cx + dx + rr, cy + dy + rr], fill=color)


def save(img: Image.Image, name: str):
    img.filter(ImageFilter.GaussianBlur(0.6)).save(OUT / name)
    print("생성:", name)


# 1) 흰 수건 더미에 남의 하늘색 수건이 섞임 (배송 사고)
img = fabric(800, 600, (233, 231, 226)); d = ImageDraw.Draw(img)
for y, c in [(55, (250, 250, 248)), (145, (247, 247, 244)), (235, (133, 178, 214)),
             (325, (250, 250, 248)), (415, (128, 172, 208))]:
    d.rounded_rectangle([80, y, 720, y + 82], 18, fill=c, outline=(202, 202, 198))
save(img, "demo_towels_mixed.png")

# 2) 연녹색 환자복 소매의 갈색(녹 계열) 얼룩
img = fabric(800, 600, (214, 226, 214)); d = ImageDraw.Draw(img)
blob(d, 430, 300, 70, (122, 88, 55)); blob(d, 530, 385, 35, (140, 100, 62), 6)
save(img, "demo_sleeve_stain.png")

# 3) 흰 수건의 누런 변색
img = fabric(800, 600, (246, 245, 240)); d = ImageDraw.Draw(img)
blob(d, 400, 280, 110, (222, 202, 140), 12)
save(img, "demo_towel_yellow.png")

# 4) 흰 테이블보의 와인 얼룩
img = fabric(800, 600, (250, 249, 246)); d = ImageDraw.Draw(img)
blob(d, 420, 300, 90, (120, 40, 70)); blob(d, 565, 235, 30, (140, 55, 85), 5)
save(img, "demo_tablecloth_wine.png")

# 5) 타월의 곰팡이 반점들
img = fabric(800, 600, (240, 240, 236)); d = ImageDraw.Draw(img)
for _ in range(24):
    blob(d, random.randint(150, 650), random.randint(120, 480), 12, (105, 110, 95), 4)
save(img, "demo_towel_mold.png")
