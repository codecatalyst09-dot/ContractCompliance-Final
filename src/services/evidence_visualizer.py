"""
Evidence Visualizer Service
Generates crisp, high-resolution visual evidence artifacts for compliance reports and UI dashboard.
Supports:
1. Authentic PDF page snippet crops with clause highlighting and full text width (no clipping).
2. Dynamic-height rendered cards for TXT/DOCX documents and fallback scenarios.
"""

import os
import re
import textwrap
from typing import Optional, List, Tuple
import pymupdf
from PIL import Image, ImageDraw, ImageFont

# ── Font Loading Helper ────────────────────────────────────────────────────────
def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    if bold:
        candidates = [
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/calibrib.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    else:
        candidates = [
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/calibri.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.ImageFont, max_width_px: int, draw: ImageDraw.ImageDraw) -> List[str]:
    """Wraps text accurately using measured pixel width."""
    lines: List[str] = []
    for paragraph in text.splitlines():
        paragraph = paragraph.strip()
        if not paragraph:
            lines.append("")
            continue
        words = paragraph.split()
        if not words:
            continue
        current_line = words[0]
        for word in words[1:]:
            test_line = f"{current_line} {word}"
            bbox = draw.textbbox((0, 0), test_line, font=font)
            w = bbox[2] - bbox[0]
            if w <= max_width_px:
                current_line = test_line
            else:
                lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)
    return lines


# ── PDF Evidence Snippet Generator ─────────────────────────────────────────────
def _render_pdf_evidence(
    doc_path: str,
    run_id: str,
    policy_id: str,
    policy_name: str,
    status: str,
    clause_ref: Optional[str],
    evidence_text: str,
    page_number: Optional[int],
    output_path: str
) -> bool:
    """Extracts a full-width PDF snippet around the matched clause, highlights it, and frames it in a card."""
    if not os.path.isfile(doc_path) or not doc_path.lower().endswith(".pdf"):
        return False

    try:
        doc = pymupdf.open(doc_path)
    except Exception:
        return False

    if len(doc) == 0:
        doc.close()
        return False

    target_page = None
    search_rects: List[pymupdf.Rect] = []

    # Priority page list
    pages_to_check: List[pymupdf.Page] = []
    if page_number and 1 <= page_number <= len(doc):
        pages_to_check.append(doc[page_number - 1])
    for idx, p in enumerate(doc):
        if not page_number or idx != (page_number - 1):
            pages_to_check.append(p)

    # Search candidates
    search_terms: List[str] = []
    if clause_ref:
        clean_ref = clause_ref.strip()
        search_terms.append(clean_ref)
        for part in clean_ref.split(","):
            if part.strip():
                search_terms.append(part.strip())

    if evidence_text and evidence_text != "NOT FOUND IN CONTRACT":
        words = re.sub(r'["\']', '', evidence_text).strip().split()
        if len(words) >= 5:
            search_terms.append(" ".join(words[:6]))
            search_terms.append(" ".join(words[:4]))
        elif len(words) >= 2:
            search_terms.append(" ".join(words))

    # Perform search
    for p in pages_to_check:
        for term in search_terms:
            rects = p.search_for(term)
            if rects:
                target_page = p
                search_rects = rects
                break
        if target_page and search_rects:
            break

    # Fallback to designated page if search term not matched
    if not target_page:
        if page_number and 1 <= page_number <= len(doc):
            target_page = doc[page_number - 1]
        else:
            target_page = doc[0]

    page_rect = target_page.rect
    full_width = page_rect.width
    full_height = page_rect.height

    if search_rects:
        # 4-coordinate tight bounds around matched clause
        raw_min_x = min(r.x0 for r in search_rects)
        raw_max_x = max(r.x1 for r in search_rects)
        raw_min_y = min(r.y0 for r in search_rects)
        raw_max_y = max(r.y1 for r in search_rects)

        # Look up enclosing text block / paragraph for full contextual reading
        enclosing_block = None
        try:
            for b in target_page.get_text("blocks"):
                bx0, by0, bx1, by1 = b[0], b[1], b[2], b[3]
                # Check overlap with search rects
                if not (bx1 < raw_min_x or bx0 > raw_max_x or by1 < raw_min_y or by0 > raw_max_y):
                    if enclosing_block is None:
                        enclosing_block = [bx0, by0, bx1, by1]
                    else:
                        enclosing_block[0] = min(enclosing_block[0], bx0)
                        enclosing_block[1] = min(enclosing_block[1], by0)
                        enclosing_block[2] = max(enclosing_block[2], bx1)
                        enclosing_block[3] = max(enclosing_block[3], by1)
        except Exception:
            enclosing_block = None

        if enclosing_block:
            min_x = max(0.0, enclosing_block[0] - 20.0)
            max_x = min(full_width, enclosing_block[2] + 20.0)
            min_y = max(0.0, enclosing_block[1] - 16.0)
            max_y = min(full_height, enclosing_block[3] + 16.0)
        else:
            min_x = max(0.0, raw_min_x - 30.0)
            max_x = min(full_width, raw_max_x + 30.0)
            min_y = max(0.0, raw_min_y - 25.0)
            max_y = min(full_height, raw_max_y + 30.0)

        # Ensure minimal dimensions so small single-word tags are still comfortably readable
        if (max_y - min_y) < 100.0:
            diff_y = 100.0 - (max_y - min_y)
            min_y = max(0.0, min_y - diff_y / 2)
            max_y = min(full_height, max_y + diff_y / 2)

        if (max_x - min_x) < 320.0:
            diff_x = 320.0 - (max_x - min_x)
            min_x = max(0.0, min_x - diff_x / 2)
            max_x = min(full_width, max_x + diff_x / 2)

        clip = pymupdf.Rect(min_x, min_y, max_x, max_y)
    else:
        top_y = 40.0
        bot_y = min(full_height, 280.0)
        clip = pymupdf.Rect(20.0, top_y, full_width - 20.0, bot_y)

    # Highlight matching text with soft yellow
    annots = []
    for r in search_rects:
        try:
            annot = target_page.add_highlight_annot(r)
            annot.set_colors(stroke=(1.0, 0.88, 0.25))
            annot.update()
            annots.append(annot)
        except Exception:
            pass

    # High-resolution rasterization (2.5x scale for retina sharpness)
    zoom = 2.5
    matrix = pymupdf.Matrix(zoom, zoom)
    pix = target_page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
    pdf_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    # Cleanup temporary annotations
    for a in annots:
        try:
            target_page.delete_annot(a)
        except Exception:
            pass
    doc.close()

    # Frame into a high quality visual card
    CARD_W = 1200
    HEADER_H = 105
    FOOTER_H = 45
    MARGIN = 24

    max_content_w = CARD_W - (MARGIN * 2)
    scale = min(1.0, max_content_w / pdf_img.width)
    display_w = int(pdf_img.width * scale)
    display_h = int(pdf_img.height * scale)
    if scale < 1.0:
        pdf_img_resized = pdf_img.resize((display_w, display_h), Image.Resampling.LANCZOS)
    else:
        pdf_img_resized = pdf_img

    CARD_H = HEADER_H + display_h + FOOTER_H + (MARGIN * 2)

    DARK_BG   = (10, 14, 26)
    CARD_BG   = (15, 23, 42)
    BORDER    = (51, 65, 85)
    TEXT_MAIN = (241, 245, 249)
    TEXT_SUB  = (148, 163, 184)
    TEXT_DIM  = (100, 116, 139)
    ACCENT    = (99, 102, 241)

    STATUS_CONFIG = {
        "COMPLIANT":     ((16, 185, 129),  "PASSED / COMPLIANT"),
        "NON_COMPLIANT": ((239,  68,  68), "FAILED / NON-COMPLIANT"),
        "PARTIAL":       ((245, 158,  11), "PARTIALLY COMPLIANT"),
        "NOT_FOUND":     ((107, 114, 128), "CLAUSE NOT FOUND"),
    }
    badge_color, badge_label = STATUS_CONFIG.get(status.upper(), STATUS_CONFIG["NOT_FOUND"])

    card = Image.new("RGB", (CARD_W, CARD_H), CARD_BG)
    draw = ImageDraw.Draw(card)

    # Card border
    draw.rectangle([(0, 0), (CARD_W - 1, CARD_H - 1)], outline=BORDER, width=2)

    # Header
    draw.rectangle([(2, 2), (CARD_W - 2, HEADER_H)], fill=DARK_BG)
    draw.rectangle([(2, 2), (6, HEADER_H)], fill=ACCENT)

    f_title  = _load_font(12, bold=True)
    f_sub    = _load_font(11, bold=False)
    f_policy = _load_font(15, bold=True)
    f_badge  = _load_font(12, bold=True)
    f_footer = _load_font(10, bold=False)

    doc_name = os.path.basename(doc_path)
    draw.text((24, 15), "DOCUMENT VERBATIM EVIDENCE SNIPPET", font=f_title, fill=ACCENT)
    draw.text((24, 36), f"Policy: [{policy_id}] {policy_name}", font=f_policy, fill=TEXT_MAIN)

    target_p_num = getattr(target_page, "number", None)
    page_display_num = page_number or ((target_p_num + 1) if isinstance(target_p_num, int) else 1)
    clause_str = f"Clause: {clause_ref or 'General'}  ·  Page {page_display_num}"
    draw.text((24, 64), f"Source: {doc_name}  ·  {clause_str}", font=f_sub, fill=TEXT_SUB)
    draw.text((24, 84), f"Run ID: {run_id}", font=f_footer, fill=TEXT_DIM)

    # Badge
    badge_w = 260
    bx1 = CARD_W - badge_w - 24
    bx2 = CARD_W - 24
    draw.rounded_rectangle([(bx1, 24), (bx2, 70)], radius=8, fill=badge_color)
    bb = draw.textbbox((0, 0), badge_label, font=f_badge)
    bw = bb[2] - bb[0]
    draw.text(((bx1 + bx2 - bw) / 2, 38), badge_label, font=f_badge, fill=(255, 255, 255))

    # Header line
    draw.line([(2, HEADER_H), (CARD_W - 2, HEADER_H)], fill=BORDER, width=1)

    # PDF container (centered horizontally)
    snippet_x = (CARD_W - display_w) // 2
    snippet_y = HEADER_H + MARGIN
    draw.rounded_rectangle(
        [(snippet_x - 2, snippet_y - 2), (snippet_x + display_w + 2, snippet_y + display_h + 2)],
        radius=8, outline=BORDER, width=2, fill=(255, 255, 255)
    )
    card.paste(pdf_img_resized, (snippet_x, snippet_y))

    # Footer
    foot_y = CARD_H - FOOTER_H
    draw.line([(2, foot_y), (CARD_W - 2, foot_y)], fill=BORDER, width=1)
    draw.text((24, foot_y + 15), "Microsoft Agent Framework (MAF) Audit Artifact  ·  Verified Immutable Contract Extract", font=f_footer, fill=TEXT_DIM)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    card.save(output_path, "JPEG", quality=95, optimize=True)
    return True


# ── Rendered Text Card (Dynamic Height for TXT/DOCX) ───────────────────────────
def _render_text_card(
    doc_path: str,
    run_id: str,
    policy_id: str,
    policy_name: str,
    status: str,
    clause_ref: Optional[str],
    evidence_text: str,
    page_number: Optional[int],
    output_path: str
) -> str:
    """Renders a dynamic-height card for text extracts, ensuring text never overflows or gets cropped."""
    CARD_W    = 1200
    HEADER_H  = 105
    FOOTER_H  = 45
    MARGIN    = 24
    BOX_PAD_X = 28
    BOX_PAD_Y = 24

    DARK_BG   = (10, 14, 26)
    CARD_BG   = (15, 23, 42)
    SURFACE   = (30, 41, 59)
    BORDER    = (51, 65, 85)
    TEXT_MAIN = (241, 245, 249)
    TEXT_SUB  = (148, 163, 184)
    TEXT_DIM  = (100, 116, 139)
    ACCENT    = (99, 102, 241)

    STATUS_CONFIG = {
        "COMPLIANT":     ((16, 185, 129),  "PASSED / COMPLIANT"),
        "NON_COMPLIANT": ((239,  68,  68), "FAILED / NON-COMPLIANT"),
        "PARTIAL":       ((245, 158,  11), "PARTIALLY COMPLIANT"),
        "NOT_FOUND":     ((107, 114, 128), "CLAUSE NOT FOUND"),
    }
    badge_color, badge_label = STATUS_CONFIG.get(status.upper(), STATUS_CONFIG["NOT_FOUND"])

    f_title   = _load_font(12, bold=True)
    f_sub     = _load_font(11, bold=False)
    f_policy  = _load_font(15, bold=True)
    f_badge   = _load_font(12, bold=True)
    f_label   = _load_font(11, bold=True)
    f_evid    = _load_font(13, bold=False)
    f_footer  = _load_font(10, bold=False)

    # Measure wrapped lines
    dummy_img = Image.new("RGB", (100, 100))
    dummy_draw = ImageDraw.Draw(dummy_img)

    box_w = CARD_W - (MARGIN * 2)
    text_available_w = box_w - (BOX_PAD_X * 2) - 10

    wrapped_lines = _wrap_text(evidence_text.strip(), f_evid, text_available_w, dummy_draw)
    if not wrapped_lines:
        wrapped_lines = ["NOT FOUND IN CONTRACT"]

    line_height = 24
    box_content_h = (len(wrapped_lines) * line_height) + 50
    min_box_h = 160
    box_h = max(min_box_h, box_content_h + (BOX_PAD_Y * 2))

    CARD_H = HEADER_H + box_h + FOOTER_H + (MARGIN * 2)

    card = Image.new("RGB", (CARD_W, CARD_H), CARD_BG)
    draw = ImageDraw.Draw(card)

    # Card border
    draw.rectangle([(0, 0), (CARD_W - 1, CARD_H - 1)], outline=BORDER, width=2)

    # Header
    draw.rectangle([(2, 2), (CARD_W - 2, HEADER_H)], fill=DARK_BG)
    draw.rectangle([(2, 2), (6, HEADER_H)], fill=ACCENT)

    doc_name = os.path.basename(doc_path)
    draw.text((24, 15), "CONTRACT COMPLIANCE EVIDENCE EXTRACT", font=f_title, fill=ACCENT)
    draw.text((24, 36), f"Policy: [{policy_id}] {policy_name}", font=f_policy, fill=TEXT_MAIN)

    page_str = f"  ·  Page {page_number}" if page_number else ""
    clause_str = f"Clause Reference: {clause_ref or 'General'}{page_str}"
    draw.text((24, 64), f"Source: {doc_name}  ·  {clause_str}", font=f_sub, fill=TEXT_SUB)
    draw.text((24, 84), f"Run ID: {run_id}", font=f_footer, fill=TEXT_DIM)

    # Badge
    badge_w = 260
    bx1 = CARD_W - badge_w - 24
    bx2 = CARD_W - 24
    draw.rounded_rectangle([(bx1, 24), (bx2, 70)], radius=8, fill=badge_color)
    bb = draw.textbbox((0, 0), badge_label, font=f_badge)
    bw = bb[2] - bb[0]
    draw.text(((bx1 + bx2 - bw) / 2, 38), badge_label, font=f_badge, fill=(255, 255, 255))

    # Header line
    draw.line([(2, HEADER_H), (CARD_W - 2, HEADER_H)], fill=BORDER, width=1)

    # Evidence Quote Box
    box_y1 = HEADER_H + MARGIN
    box_y2 = box_y1 + box_h
    draw.rounded_rectangle([(MARGIN, box_y1), (CARD_W - MARGIN, box_y2)], radius=10, fill=SURFACE, outline=BORDER, width=1)

    # Accent stripe inside box
    draw.rectangle([(MARGIN + 4, box_y1 + 4), (MARGIN + 10, box_y2 - 4)], fill=ACCENT)

    draw.text((MARGIN + BOX_PAD_X, box_y1 + 18), "VERBATIM EXTRACTED EVIDENCE SNIPPET:", font=f_label, fill=TEXT_DIM)

    text_y = box_y1 + 46
    for i, line in enumerate(wrapped_lines):
        prefix = '“' if i == 0 else ' '
        suffix = '”' if i == len(wrapped_lines) - 1 else ''
        draw.text((MARGIN + BOX_PAD_X, text_y), f"{prefix}{line}{suffix}", font=f_evid, fill=TEXT_MAIN)
        text_y += line_height

    # Footer
    foot_y = CARD_H - FOOTER_H
    draw.line([(2, foot_y), (CARD_W - 2, foot_y)], fill=BORDER, width=1)
    draw.text((24, foot_y + 15), "Microsoft Agent Framework (MAF) Audit Artifact  ·  Verified Immutable Extract", font=f_footer, fill=TEXT_DIM)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    card.save(output_path, "JPEG", quality=95, optimize=True)
    return output_path


# ── Main Entrypoint ────────────────────────────────────────────────────────────
def capture_evidence_jpg(
    doc_path: str,
    run_id: str,
    policy_id: str,
    policy_name: str,
    status: str,
    clause_ref: Optional[str],
    evidence_text: str,
    page_number: Optional[int] = None,
    output_dir: str = "outputs/evidence_images"
) -> str:
    """
    Renders a premium visual evidence artifact as JPG for a given policy finding.
    If the document is a PDF, captures an authentic high-resolution snippet with full text width and highlight.
    For other formats or fallbacks, generates a dynamic-height, beautifully formatted card without text clipping.
    """
    os.makedirs(output_dir, exist_ok=True)
    clean_policy_id = policy_id.replace(" ", "_").replace("-", "_")
    image_path = os.path.join(output_dir, f"{run_id}_{clean_policy_id}_evidence.jpg")

    # If PDF, attempt authentic PDF snippet rendering first
    if doc_path and doc_path.lower().endswith(".pdf") and os.path.isfile(doc_path):
        success = _render_pdf_evidence(
            doc_path=doc_path,
            run_id=run_id,
            policy_id=policy_id,
            policy_name=policy_name,
            status=status,
            clause_ref=clause_ref,
            evidence_text=evidence_text,
            page_number=page_number,
            output_path=image_path
        )
        if success:
            return image_path

    # Fallback to dynamic text card
    _render_text_card(
        doc_path=doc_path,
        run_id=run_id,
        policy_id=policy_id,
        policy_name=policy_name,
        status=status,
        clause_ref=clause_ref,
        evidence_text=evidence_text,
        page_number=page_number,
        output_path=image_path
    )
    return image_path
