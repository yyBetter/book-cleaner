"""High-fidelity synthetic paper book scan generator for offline testing and demos."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Sequence

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


_FONT_CANDIDATES = (
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _draw_text_page(
    width: int,
    height: int,
    heading: str,
    paragraphs: Sequence[str],
    *,
    bg_rgb: tuple[int, int, int] = (250, 248, 244),
    margin_x: int = 110,
    margin_y: int = 120,
    page_footer: str = "",
) -> np.ndarray:
    img = Image.new("RGB", (width, height), bg_rgb)
    draw = ImageDraw.Draw(img)
    title_font = _load_font(44)
    body_font = _load_font(28)
    footer_font = _load_font(22)

    y = margin_y
    if heading:
        draw.text((margin_x, y), heading, fill=(24, 24, 24), font=title_font)
        y += 84

    for para in paragraphs:
        # Wrap conservatively by character width for crisp layout
        line = ""
        max_w = width - margin_x * 2
        for ch in para:
            cand = line + ch
            bbox = draw.textbbox((0, 0), cand, font=body_font)
            if bbox[2] - bbox[0] > max_w and line:
                draw.text((margin_x, y), line, fill=(32, 32, 32), font=body_font)
                y += 44
                line = ch
            else:
                line = cand
        if line:
            draw.text((margin_x, y), line, fill=(32, 32, 32), font=body_font)
            y += 44
        y += 22

    if page_footer:
        bbox = draw.textbbox((0, 0), page_footer, font=footer_font)
        fw = bbox[2] - bbox[0]
        draw.text(((width - fw) // 2, height - 64), page_footer, fill=(90, 90, 90), font=footer_font)

    rgb = np.asarray(img, dtype=np.uint8)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _encode_jpg(bgr: np.ndarray, quality: int = 92) -> bytes:
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("Failed to encode demo JPEG")
    return buf.tobytes()


def generate_demo_pages() -> list[tuple[str, bytes]]:
    """Return 6 realistic synthetic book page scans: (filename, jpeg_bytes)."""
    pages: list[tuple[str, bytes]] = []

    # 1. Yellowed paper + spine shadow gradient + dark desk margin perspective tilt
    p1 = _draw_text_page(
        1100,
        1500,
        "第一章 纸质书籍数字化清洗工艺",
        [
            "在十万页级纸质档案与图书数字化流水线中，翻拍采集与图像清洗必须彻底解耦。",
            "操作员每次翻页后，采集端仅执行毫秒级清晰度、过曝与重复帧检测并立刻落盘队列。",
            "后台清洗引擎随后自动完成切边矫正、书脊阴影去除、泛黄底色漂白以及边缘手指擦除。",
            "Book Cleaner Pipeline verifies local OpenCV illumination flattening and offline RapidOCR.",
        ],
        bg_rgb=(242, 228, 196),
        page_footer="12",
    )
    # Add uneven left spine shadow gradient
    x_grad = np.linspace(0.56, 1.0, p1.shape[1], dtype=np.float32)
    shadow_curve = np.where(x_grad < 0.78, x_grad * 0.92, 1.0)
    shadow_2d = np.clip(np.linspace(0.58, 1.0, p1.shape[1], dtype=np.float32) ** 0.55, 0.55, 1.0)
    p1 = np.clip(p1.astype(np.float32) * shadow_2d[None, :, None], 0, 255).astype(np.uint8)
    # Place on dark desk with slight perspective tilt
    desk = np.full((1660, 1260, 3), (38, 42, 48), dtype=np.uint8)
    src = np.array([[0, 0], [1099, 0], [1099, 1499], [0, 1499]], dtype=np.float32)
    dst = np.array([[88, 72], [1170, 96], [1182, 1575], [70, 1558]], dtype=np.float32)
    mat = cv2.getPerspectiveTransform(src, dst)
    warped_p1 = cv2.warpPerspective(p1, mat, (1260, 1660), borderMode=cv2.BORDER_CONSTANT, borderValue=(38, 42, 48))
    pages.append(("01_yellowed_shadow_tilt.jpg", _encode_jpg(warped_p1)))

    # 2. Curved page + skin-colored thumb pressing right margin
    p2 = _draw_text_page(
        1100,
        1500,
        "第二章 书页弯曲展平与手指擦除",
        [
            "厚重精装书籍在非破坏性翻拍时，靠近中缝处的文字行常出现向上或向下弯曲畸变。",
            "算法通过追踪正文行墨迹质心的横向偏移拟合平滑多项式曲面，实现零撕裂展平。",
            "当操作员拇指短暂按压书页外侧白边时，边缘肤色掩膜会精准定位并由周边纸色自然修复。",
            "Curvature dewarping and edge finger inpainting keep margins clean without deleting text.",
        ],
        bg_rgb=(248, 244, 236),
        margin_x=130,
    )
    h2, w2 = p2.shape[:2]
    xn = np.linspace(0.0, 1.0, w2, dtype=np.float32)
    arch = -26.0 * np.sin(np.pi * xn)
    gx = np.tile(np.arange(w2, dtype=np.float32), (h2, 1))
    gy = np.arange(h2, dtype=np.float32)[:, None] + arch[None, :]
    p2_curved = cv2.remap(p2, gx, gy, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    # Paint a realistic skin-colored thumb touching right edge (BGR: ~122, 156, 206)
    cv2.ellipse(p2_curved, (w2 - 35, 760), (92, 46), -8, 0, 360, (124, 158, 208), -1)
    cv2.rectangle(p2_curved, (w2 - 75, 714), (w2 - 1, 806), (124, 158, 208), -1)
    pages.append(("02_curved_with_finger.jpg", _encode_jpg(p2_curved)))

    # 3. Landscape 2-page spread with dark center spine gutter
    left_p = _draw_text_page(
        900,
        1250,
        "第三章 双页连拍与中缝拆页",
        [
            "在俯拍架翻拍模式下，一次快门即可同时拍下左右两页跨页画面，采集速度翻倍。",
            "系统在中缝区域自动定位文字行能量最低的装订谷底，将其精准拆分为左页与右页。",
            "Left spread page demonstrates automatic gutter detection and independent OCR routing.",
        ],
        bg_rgb=(246, 240, 224),
        margin_x=80,
        page_footer="24",
    )
    right_p = _draw_text_page(
        900,
        1250,
        "第四章 离线识别与双格式导出",
        [
            "所有识别均依托本地 ONNX 推理引擎完成，十万页处理无需支付任何外部接口费用。",
            "清洗后的图片与透明中文检索层共同打包为可搜索 PDF，并同步输出 Markdown 与 EPUB。",
            "Right spread page produces clean searchable PDF, Markdown, EPUB 3, and RAG JSONL.",
        ],
        bg_rgb=(246, 240, 224),
        margin_x=80,
        page_footer="25",
    )
    spread = np.hstack([left_p, right_p])
    # Darken center spine gutter around x=900
    xs = np.arange(spread.shape[1], dtype=np.float32)
    gutter_shade = 1.0 - 0.52 * np.exp(-((xs - 900.0) / 36.0) ** 2)
    spread = np.clip(spread.astype(np.float32) * gutter_shade[None, :, None], 0, 255).astype(np.uint8)
    pages.append(("03_double_spread.jpg", _encode_jpg(spread)))

    # 4. Clean rotated page with structured content + 3-column parameter table
    p4 = _draw_text_page(
        1100,
        1500,
        "第五章 质量门禁与断点续传",
        [
            "数据库采用 SQLite WAL 预写日志架构与两位十六进制分片目录，制定日期：2026年3月15日。",
            "清晰度下限：80.0，1. 清晰度评分低于阈值时，工位立即蜂鸣告警并标记为需人工复核。",
            "2. 感知哈希 dHash 自动捕捉连续误拍重复页，防止废片进入后续精校队列。",
        ],
        bg_rgb=(252, 252, 250),
    )
    # Paint a crisp 3-column table on the lower portion of page 4
    p4_pil = Image.fromarray(cv2.cvtColor(p4, cv2.COLOR_BGR2RGB))
    d4 = ImageDraw.Draw(p4_pil)
    tbl_font = _load_font(26)
    table_rows = [
        ("工序阶段", "目标指标", "备注说明"),
        ("L0采集质检", "清晰度 >= 80", "模糊即时蜂鸣"),
        ("L1图像清洗", "阴影波动 < 5", "保留彩图印章"),
        ("L2离线识别", "置信度 >= 82%", "低分自动标黄"),
    ]
    col_xs = [125, 425, 715]
    y_tbl = 780
    for r_i, row_vals in enumerate(table_rows):
        d4.rectangle([110, y_tbl - 10, 990, y_tbl + 46], outline=(110, 110, 110), width=2)
        for c_i, cell_txt in enumerate(row_vals):
            d4.text((col_xs[c_i], y_tbl), cell_txt, fill=(24, 24, 24), font=tbl_font)
        y_tbl += 56
    p4 = cv2.cvtColor(np.asarray(p4_pil, dtype=np.uint8), cv2.COLOR_RGB2BGR)
    rot_m = cv2.getRotationMatrix2D((550, 750), 3.8, 1.0)
    p4_rot = cv2.warpAffine(p4, rot_m, (1100, 1500), borderMode=cv2.BORDER_CONSTANT, borderValue=(252, 252, 250))
    pages.append(("04_tilted_clean_page.jpg", _encode_jpg(p4_rot)))

    # 5. Intentionally blurry page to trigger L0 blur QA & NEEDS_REVIEW
    p5 = _draw_text_page(
        1100,
        1500,
        "第六章 模糊废片自动拦截测试",
        [
            "本页故意施加了强烈的运动与失焦模糊，用于验证采集端毫秒级质检与复核分流机制。",
            "操作员在翻拍台听到提示音后，可直接按退格键删除重拍，不耽误整本翻拍节奏。",
        ],
        bg_rgb=(248, 248, 246),
    )
    p5_blur = cv2.GaussianBlur(p5, (19, 19), 6.5)
    pages.append(("05_blurry_test_shot.jpg", _encode_jpg(p5_blur)))

    # 6. Near-duplicate of page 4 to verify dHash duplicate warning
    p6_dup = np.clip(p4_rot.astype(np.int16) - 6, 0, 255).astype(np.uint8)
    pages.append(("06_duplicate_test_shot.jpg", _encode_jpg(p6_dup)))

    return pages


def write_demo_book_to_dir(out_dir: Path | str) -> list[Path]:
    """Generate and save demo scan files into `out_dir`."""
    target = Path(out_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fname, data in generate_demo_pages():
        p = target / fname
        p.write_bytes(data)
        written.append(p)
    return written
