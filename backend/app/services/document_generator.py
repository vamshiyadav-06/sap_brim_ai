"""
Creates downloadable files (Word, PDF, Excel, PowerPoint, CSV, text, Markdown, HTML) from an answer.

When a user asks the assistant to create a document, the model writes the document's content as Markdown; this
module turns that Markdown into the requested file, stores it for its owner, and the chat shows a short message
with a download link instead of the whole text. Everything here is free and local: python-docx, PyMuPDF,
python-pptx and XlsxWriter, no external service.

Markdown is parsed once into blocks (headings, paragraphs, list items, tables, code, rules) and each renderer
walks those blocks, so all formats are built from the same structure.
"""

import csv
import html as html_lib
import io
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.app.config import settings
from backend.app.services.evidence_markers import strip_markers

logger = logging.getLogger(__name__)

MAX_GENERATED_BYTES = 20 * 1024 * 1024
MAX_FORMATS_PER_REQUEST = 3
_FILE_ID = re.compile(r"^[0-9a-f]{32}$")

# format -> (extension, media type, label shown to the user)
FORMATS: Dict[str, Tuple[str, str, str]] = {
    "docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Word document"),
    "pdf": ("pdf", "application/pdf", "PDF"),
    "xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Excel workbook"),
    "pptx": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "PowerPoint presentation"),
    "csv": ("csv", "text/csv", "CSV file"),
    "txt": ("txt", "text/plain", "text file"),
    "md": ("md", "text/markdown", "Markdown file"),
    "html": ("html", "text/html", "HTML page"),
}


class DocumentGenerationError(ValueError):
    """The file could not be created; the message is safe to show to the user."""


# ---------------------------------------------------------------------------------------------------------
# Is a file being asked for?
# ---------------------------------------------------------------------------------------------------------

_CREATE_VERB = re.compile(
    r"\b(create|make|generate|write|prepare|draft|build|produce|export|convert|compile|put (it|this|that|them) (in|into)|"
    r"give me|send me|save (it|this|that)|turn (it|this|that) into|i (need|want)|provide|download)\b", re.IGNORECASE)
_QUESTION_START = re.compile(r"^\s*(what|how|why|where|when|which|who|is|are|does|do|can i|could i|should i)\b", re.IGNORECASE)
_FORMAT_WORDS = [
    ("pptx", r"\b(power ?point|pptx?|slide ?deck|slides|presentation|deck)\b"),
    ("xlsx", r"\b(excel|xlsx?|spreadsheet|work ?sheet)\b"),
    ("pdf", r"\bpdf\b"),
    ("csv", r"\bcsv\b"),
    ("docx", r"\b(word (document|doc|file)|docx?|ms word|microsoft word|word format)\b"),
    ("html", r"\bhtml (file|page|document)\b|\.html\b"),
    ("md", r"\bmarkdown (file|document)\b|\.md\b"),
    ("txt", r"\b(text|txt|plain text) file\b|\.txt\b"),
]


def detect_formats(text: str) -> List[str]:
    """
    Rule-based fallback, used only when the LLM intent analysis is unavailable: the formats a message asks to
    be created ("make me a PDF about ...", "export this to Excel"). Questions about a format are not requests.
    """
    text = (text or "").strip()
    if not text or _QUESTION_START.match(text) or not _CREATE_VERB.search(text):
        return []
    found = [fmt for fmt, pattern in _FORMAT_WORDS if re.search(pattern, text, re.IGNORECASE)]
    if not found and re.search(r"\b(a|the|an)\s+(document|report|doc)\b", text, re.IGNORECASE) \
            and re.search(r"\b(create|make|generate|write|prepare|draft|produce)\b", text, re.IGNORECASE):
        found = ["docx"]
    return found[:MAX_FORMATS_PER_REQUEST]


def requested_formats(analysis, query: str) -> List[str]:
    """The files a message asks for: from the LLM intent analysis, or the rule-based fallback when there is none."""
    if not getattr(settings, "DOCUMENT_GENERATION_ENABLED", True):
        return []
    if analysis is not None:
        return normalize_formats(getattr(analysis, "output_formats", []))
    return detect_formats(query)


def normalize_formats(values: Any) -> List[str]:
    out: List[str] = []
    for value in values if isinstance(values, (list, tuple)) else []:
        key = str(value).strip().lower().lstrip(".")
        key = {"word": "docx", "doc": "docx", "excel": "xlsx", "xls": "xlsx", "powerpoint": "pptx", "ppt": "pptx",
               "markdown": "md", "text": "txt", "htm": "html"}.get(key, key)
        if key in FORMATS and key not in out:
            out.append(key)
    return out[:MAX_FORMATS_PER_REQUEST]


def content_instructions(formats: List[str]) -> str:
    """What the model must write when a file is requested: the document's content only, shaped for the format."""
    labels = " and ".join(FORMATS[f][2] for f in formats)
    lines = [
        f"DOCUMENT REQUEST: the user asked for a {labels}. Write the complete content of that document, in Markdown, and nothing else:",
        "- Start with one H1 title, then structured sections (## headings), concise paragraphs, lists and tables where they help.",
        "- No chat preamble (\"Sure\", \"Here is\"), no closing remarks or offers, no mention of files, downloads or links, and no citation markers.",
        "- Make it complete and ready to use as the final document, at the depth the request implies.",
    ]
    if any(f in ("xlsx", "csv") for f in formats):
        lines.append("- For the spreadsheet/CSV: put the data in Markdown tables with a header row (one table per sheet, with a short ## heading before each). Keep cells short; no merged cells.")
    if "pptx" in formats:
        lines.append("- For the presentation: # deck title, then one ## heading per slide with 3-6 short bullet points under it (at most 12 words each). Add a table only where data needs it.")
    if "txt" in formats:
        lines.append("- For the text file: plain prose with minimal formatting.")
    return "\n".join(lines)


def query_with_instructions(query: str, formats: List[str]) -> str:
    """The question as given to a direct (non-retrieval) model call, with the content instructions appended."""
    return f"{query}\n\n{content_instructions(formats)}"


# ---------------------------------------------------------------------------------------------------------
# Markdown -> blocks
# ---------------------------------------------------------------------------------------------------------

@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    href: Optional[str] = None


@dataclass
class Block:
    kind: str  # heading | paragraph | item | table | code | rule
    level: int = 0           # heading level, or list depth for items
    runs: List[Run] = field(default_factory=list)
    ordered: bool = False
    number: int = 0
    rows: List[List[List[Run]]] = field(default_factory=list)  # table: first row is the header
    text: str = ""           # code


def _plain(runs: List[Run]) -> str:
    return "".join(r.text for r in runs)


def _inline_runs(token) -> List[Run]:
    runs: List[Run] = []
    bold = italic = 0
    hrefs: List[str] = []
    for child in token.children or []:
        kind = child.type
        if kind == "text":
            runs.append(Run(child.content, bold > 0, italic > 0, False, hrefs[-1] if hrefs else None))
        elif kind == "code_inline":
            runs.append(Run(child.content, bold > 0, italic > 0, True, hrefs[-1] if hrefs else None))
        elif kind == "strong_open":
            bold += 1
        elif kind == "strong_close":
            bold = max(0, bold - 1)
        elif kind == "em_open":
            italic += 1
        elif kind == "em_close":
            italic = max(0, italic - 1)
        elif kind == "link_open":
            hrefs.append(child.attrGet("href") or "")
        elif kind == "link_close":
            if hrefs:
                hrefs.pop()
        elif kind == "softbreak":
            runs.append(Run(" "))
        elif kind == "hardbreak":
            runs.append(Run("\n"))
        elif kind == "image":
            runs.append(Run(child.content or ""))
    return runs


def parse_markdown(markdown: str) -> List[Block]:
    from markdown_it import MarkdownIt

    tokens = MarkdownIt("commonmark", {"html": False}).enable("table").parse(markdown or "")
    blocks: List[Block] = []
    lists: List[List[Any]] = []  # [ordered, counter]
    heading_level = 0
    in_item = False
    row: Optional[List[List[Run]]] = None
    table: Optional[Block] = None
    item_has_text = False

    for token in tokens:
        kind = token.type
        if kind in ("bullet_list_open", "ordered_list_open"):
            lists.append([kind == "ordered_list_open", int(token.attrGet("start") or 1) if kind == "ordered_list_open" else 0])
        elif kind in ("bullet_list_close", "ordered_list_close"):
            if lists:
                lists.pop()
        elif kind == "list_item_open":
            in_item, item_has_text = True, False
            if lists and lists[-1][0]:
                lists[-1][1] += 1
        elif kind == "list_item_close":
            in_item = False
        elif kind == "heading_open":
            heading_level = int(token.tag[1])
        elif kind == "table_open":
            table = Block("table")
        elif kind == "tr_open":
            row = []
        elif kind == "tr_close":
            if table is not None and row is not None:
                table.rows.append(row)
            row = None
        elif kind == "table_close":
            if table is not None and table.rows:
                blocks.append(table)
            table = None
        elif kind == "inline":
            runs = _inline_runs(token)
            if row is not None:
                row.append(runs)
            elif heading_level:
                blocks.append(Block("heading", level=heading_level, runs=runs))
                heading_level = 0
            elif in_item and lists:
                ordered = lists[-1][0]
                blocks.append(Block("item", level=len(lists) - 1, runs=runs, ordered=ordered,
                                    number=lists[-1][1] if ordered else 0) if not item_has_text
                              else Block("paragraph", level=len(lists), runs=runs))
                item_has_text = True
            else:
                blocks.append(Block("paragraph", runs=runs))
        elif kind in ("fence", "code_block"):
            blocks.append(Block("code", text=token.content.rstrip("\n")))
        elif kind == "hr":
            blocks.append(Block("rule"))
    return blocks


def _clean_markdown(markdown: str) -> str:
    return strip_markers(markdown or "").strip()


def title_of(markdown: str, fallback: str = "Document") -> str:
    for block in parse_markdown(_clean_markdown(markdown)):
        if block.kind == "heading":
            title = _plain(block.runs).strip()
            if title:
                return title[:120]
    return fallback


def _safe_filename(title: str, ext: str) -> str:
    stem = re.sub(r"[^\w\- ]+", "", title, flags=re.UNICODE).strip().replace(" ", "_")[:60].strip("_") or "document"
    return f"{stem}.{ext}"


def _outline(blocks: List[Block], limit: int = 8) -> List[str]:
    return [_plain(b.runs).strip() for b in blocks if b.kind == "heading" and b.level == 2 and _plain(b.runs).strip()][:limit]


# ---------------------------------------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------------------------------------

def _formula_safe(value: str) -> str:
    """CSV/Excel treat a leading = + - @ as a formula; text from a model is never allowed to become one."""
    if value and value[0] in "=+-@\t\r" and not re.fullmatch(r"[-+]?\d[\d,]*(\.\d+)?%?", value.strip()):
        return "'" + value
    return value


def render_markdown(markdown: str) -> bytes:
    return (_clean_markdown(markdown) + "\n").encode("utf-8")


def _html_body(markdown: str) -> str:
    from markdown_it import MarkdownIt
    return MarkdownIt("commonmark", {"html": False}).enable("table").render(_clean_markdown(markdown))


_CSS = """
body{font-family:Segoe UI,Arial,Helvetica,sans-serif;font-size:11pt;line-height:1.5;color:#1b1b1f}
h1{font-size:22pt;color:#c2410c;margin:0 0 10pt} h2{font-size:15pt;color:#1f2937;margin:16pt 0 6pt}
h3{font-size:12.5pt;color:#374151;margin:12pt 0 4pt} p{margin:0 0 7pt}
table{border-collapse:collapse;margin:8pt 0} th{background:#fdeadb;font-weight:bold;text-align:left}
th,td{border:1px solid #c9ced6;padding:4pt 6pt;vertical-align:top}
code{font-family:Consolas,Courier New,monospace;background:#f3f4f6}
pre{background:#f3f4f6;padding:6pt;font-family:Consolas,Courier New,monospace;font-size:9pt}
blockquote{color:#4b5563;border-left:3px solid #e5e7eb;margin-left:0;padding-left:8pt}
"""


def render_html(markdown: str, title: str) -> bytes:
    page = (
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html_lib.escape(title)}</title><style>{_CSS}body{{max-width:820px;margin:32px auto;padding:0 16px}}</style></head>"
        f"<body>{_html_body(markdown)}</body></html>"
    )
    return page.encode("utf-8")


def render_pdf(markdown: str, title: str) -> bytes:
    import fitz

    story = fitz.Story(html=_html_body(markdown), user_css=_CSS)
    buffer = io.BytesIO()
    writer = fitz.DocumentWriter(buffer)
    page_rect = fitz.paper_rect("a4")
    content_rect = page_rect + (54, 54, -54, -60)
    more = True
    pages = 0
    while more:
        device = writer.begin_page(page_rect)
        more, _ = story.place(content_rect)
        story.draw(device)
        writer.end_page()
        pages += 1
        if pages > 300:
            break
    writer.close()
    document = fitz.open("pdf", buffer.getvalue())
    document.set_metadata({"title": title, "author": "Clyptus.AI", "creator": "Clyptus.AI"})
    for index, page in enumerate(document, 1):
        page.insert_text((page.rect.width / 2 - 14, page.rect.height - 30), f"{index} / {len(document)}", fontsize=8, color=(0.45, 0.45, 0.45))
    return document.tobytes(garbage=3, deflate=True)


def render_txt(markdown: str) -> bytes:
    lines: List[str] = []
    for block in parse_markdown(_clean_markdown(markdown)):
        if block.kind == "heading":
            text = _plain(block.runs).strip()
            lines += ["", text.upper() if block.level == 1 else text, ("=" if block.level == 1 else "-") * min(len(text), 80), ""]
        elif block.kind == "paragraph":
            lines += [("  " * block.level) + _plain(block.runs).strip(), ""]
        elif block.kind == "item":
            marker = f"{block.number}." if block.ordered else "-"
            lines.append(f"{'  ' * block.level}{marker} {_plain(block.runs).strip()}")
        elif block.kind == "table":
            widths = [max(len(_plain(r[c])) if c < len(r) else 0 for r in block.rows) for c in range(max(len(r) for r in block.rows))]
            for i, r in enumerate(block.rows):
                lines.append(" | ".join((_plain(r[c]) if c < len(r) else "").ljust(widths[c]) for c in range(len(widths))).rstrip())
                if i == 0:
                    lines.append("-+-".join("-" * w for w in widths))
            lines.append("")
        elif block.kind == "code":
            lines += [block.text, ""]
        elif block.kind == "rule":
            lines += ["-" * 40, ""]
    return (re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n").encode("utf-8")


def _table_cells(table: Block) -> List[List[str]]:
    width = max(len(r) for r in table.rows)
    return [[_plain(r[c]).strip() if c < len(r) else "" for c in range(width)] for r in table.rows]


def render_csv(markdown: str) -> bytes:
    blocks = parse_markdown(_clean_markdown(markdown))
    table = next((b for b in blocks if b.kind == "table"), None)
    rows = _table_cells(table) if table else [["Content"]] + [[_plain(b.runs).strip() or b.text] for b in blocks if b.kind in ("paragraph", "item", "heading", "code")]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    for row in rows:
        writer.writerow([_formula_safe(cell) for cell in row])
    return ("﻿" + buffer.getvalue()).encode("utf-8")  # the BOM makes Excel read it as UTF-8


def _sheet_name(raw: str, used: set) -> str:
    name = re.sub(r"[\[\]:*?/\\]", " ", raw).strip()[:31] or "Sheet"
    base, n = name, 2
    while name.lower() in used:
        suffix = f" {n}"
        name, n = base[:31 - len(suffix)] + suffix, n + 1
    used.add(name.lower())
    return name


def _number_or_none(text: str):
    cleaned = text.replace(",", "") if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", text) else text
    if re.fullmatch(r"-?(0|[1-9]\d*)", cleaned):
        return int(cleaned) if len(cleaned) < 16 else None
    if re.fullmatch(r"-?(0|[1-9]\d*)\.\d+", cleaned):
        return float(cleaned)
    return None


def render_xlsx(markdown: str, title: str) -> bytes:
    import xlsxwriter

    blocks = parse_markdown(_clean_markdown(markdown))
    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False})
    workbook.set_properties({"title": title, "author": "Clyptus.AI"})
    head = workbook.add_format({"bold": True, "bg_color": "#FDEADB", "border": 1, "text_wrap": True, "valign": "top"})
    cell = workbook.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    bold = workbook.add_format({"bold": True})
    used: set = set()

    def write_grid(sheet, rows: List[List[str]]):
        widths = [8] * max(len(r) for r in rows)
        for r_index, values in enumerate(rows):
            for c_index, text in enumerate(values):
                number = _number_or_none(text) if r_index else None
                if number is not None:
                    sheet.write_number(r_index, c_index, number, cell)
                else:
                    sheet.write_string(r_index, c_index, text, head if r_index == 0 else cell)
                widths[c_index] = min(60, max(widths[c_index], len(text) + 2))
        for c_index, width in enumerate(widths):
            sheet.set_column(c_index, c_index, width)
        sheet.freeze_panes(1, 0)
        if len(rows) > 1:
            sheet.autofilter(0, 0, len(rows) - 1, len(rows[0]) - 1)

    tables: List[Tuple[str, Block]] = []
    last_heading = ""
    for block in blocks:
        if block.kind == "heading":
            last_heading = _plain(block.runs).strip()
        elif block.kind == "table":
            tables.append((last_heading or f"Table {len(tables) + 1}", block))
            last_heading = ""
    if tables:
        for name, table in tables:
            write_grid(workbook.add_worksheet(_sheet_name(name, used)), _table_cells(table))
    else:
        sheet = workbook.add_worksheet(_sheet_name("Content", used))
        sheet.set_column(0, 0, 100, workbook.add_format({"text_wrap": True, "valign": "top"}))
        row = 0
        for block in blocks:
            if block.kind in ("heading", "paragraph", "item", "code"):
                text = _plain(block.runs).strip() if block.kind != "code" else block.text
                if block.kind == "item":
                    text = ("  " * block.level) + ("•" if not block.ordered else f"{block.number}.") + " " + text
                sheet.write_string(row, 0, text, bold if block.kind == "heading" else None)
                row += 1
    workbook.close()
    return buffer.getvalue()


def _set_cell_shading(cell, hex_fill: str):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), hex_fill)
    cell._tc.get_or_add_tcPr().append(shading)


def _add_hyperlink(paragraph, text: str, url: str, bold: bool, italic: bool):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE

    rel_id = paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel_id)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    style = OxmlElement("w:rStyle")
    style.set(qn("w:val"), "Hyperlink")
    props.append(style)
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    props.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    props.append(underline)
    if bold:
        props.append(OxmlElement("w:b"))
    if italic:
        props.append(OxmlElement("w:i"))
    run.append(props)
    node = OxmlElement("w:t")
    node.text = text
    node.set(qn("xml:space"), "preserve")
    run.append(node)
    link.append(run)
    paragraph._p.append(link)


def _docx_runs(paragraph, runs: List[Run]):
    from docx.shared import Pt
    for run in runs:
        if run.href and re.match(r"^(https?:|mailto:)", run.href, re.IGNORECASE):
            _add_hyperlink(paragraph, run.text, run.href, run.bold, run.italic)
            continue
        piece = paragraph.add_run(run.text)
        piece.bold, piece.italic = run.bold or None, run.italic or None
        if run.code:
            piece.font.name = "Consolas"
            piece.font.size = Pt(9.5)


def render_docx(markdown: str, title: str) -> bytes:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    document = Document()
    document.core_properties.title = title
    document.core_properties.author = "Clyptus.AI"
    normal = document.styles["Normal"]
    normal.font.name, normal.font.size = "Calibri", Pt(11)
    for section in document.sections:
        section.left_margin = section.right_margin = Inches(1)

    for block in parse_markdown(_clean_markdown(markdown)):
        if block.kind == "heading":
            heading = document.add_heading(level=min(block.level, 4))
            _docx_runs(heading, block.runs)
            if block.level == 1:
                for run in heading.runs:
                    run.font.color.rgb = RGBColor(0xC2, 0x41, 0x0C)
        elif block.kind == "paragraph":
            paragraph = document.add_paragraph()
            if block.level:
                paragraph.paragraph_format.left_indent = Inches(0.25 * (block.level + 1))
            _docx_runs(paragraph, block.runs)
        elif block.kind == "item":
            paragraph = document.add_paragraph()
            indent = 0.25 * (block.level + 1)
            paragraph.paragraph_format.left_indent = Inches(indent + 0.2)
            paragraph.paragraph_format.first_line_indent = Inches(-0.2)
            paragraph.paragraph_format.space_after = Pt(2)
            marker = f"{block.number}. " if block.ordered else ("• " if block.level == 0 else "– ")
            paragraph.add_run(marker)
            _docx_runs(paragraph, block.runs)
        elif block.kind == "table":
            rows = _table_cells(block)
            table = document.add_table(rows=len(rows), cols=len(rows[0]))
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            for r_index, values in enumerate(rows):
                for c_index, text in enumerate(values):
                    cell = table.cell(r_index, c_index)
                    cell.text = ""
                    run = cell.paragraphs[0].add_run(text)
                    run.font.size = Pt(10)
                    if r_index == 0:
                        run.bold = True
                        _set_cell_shading(cell, "FDEADB")
            document.add_paragraph()
        elif block.kind == "code":
            paragraph = document.add_paragraph()
            run = paragraph.add_run(block.text)
            run.font.name, run.font.size = "Consolas", Pt(9)
            shading = OxmlElement("w:shd")
            shading.set(qn("w:val"), "clear")
            shading.set(qn("w:fill"), "F3F4F6")
            paragraph._p.get_or_add_pPr().append(shading)
        elif block.kind == "rule":
            paragraph = document.add_paragraph()
            border = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            for key, value in (("val", "single"), ("sz", "6"), ("space", "1"), ("color", "C9CED6")):
                bottom.set(qn(f"w:{key}"), value)
            border.append(bottom)
            paragraph._p.get_or_add_pPr().append(border)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def render_pptx(markdown: str, title: str) -> bytes:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    blocks = parse_markdown(_clean_markdown(markdown))
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = Inches(13.333), Inches(7.5)
    presentation.core_properties.title = title
    presentation.core_properties.author = "Clyptus.AI"
    accent = RGBColor(0xC2, 0x41, 0x0C)

    def style_runs(paragraph, runs: List[Run], size: int):
        for run in runs:
            piece = paragraph.add_run()
            piece.text = run.text
            piece.font.size = Pt(size)
            piece.font.bold = run.bold or None
            piece.font.italic = run.italic or None
            if run.code:
                piece.font.name = "Consolas"

    def place_title(slide, text: str, size: int = 32):
        shape = slide.shapes.title
        shape.left, shape.top, shape.width, shape.height = Inches(0.7), Inches(0.4), Inches(11.9), Inches(1.1)
        shape.text_frame.text = text
        shape.text_frame.word_wrap = True
        for paragraph in shape.text_frame.paragraphs:
            for run in paragraph.runs:
                run.font.size, run.font.bold, run.font.color.rgb = Pt(size), True, accent

    def bullet_slide(heading: str, items: List[Block]):
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        place_title(slide, heading)
        body = slide.placeholders[1]
        body.left, body.top, body.width, body.height = Inches(0.9), Inches(1.7), Inches(11.5), Inches(5.2)
        frame = body.text_frame
        frame.word_wrap = True
        total = sum(len(_plain(i.runs)) for i in items)
        size = 24 if total < 350 else 20 if total < 650 else 16
        for index, item in enumerate(items):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.level = min(item.level, 3) if item.kind == "item" else 0
            runs = item.runs if item.kind != "heading" else [Run(_plain(item.runs), bold=True)]
            style_runs(paragraph, runs, size)

    def table_slide(heading: str, table_block: Block):
        rows = _table_cells(table_block)[:14]
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        place_title(slide, heading)
        shape = slide.shapes.add_table(len(rows), len(rows[0]), Inches(0.7), Inches(1.7), Inches(11.9), Inches(0.4 * len(rows)))
        for r_index, values in enumerate(rows):
            for c_index, text in enumerate(values):
                cell = shape.table.cell(r_index, c_index)
                cell.text = text
                for paragraph in cell.text_frame.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = Pt(14)
                        run.font.bold = True if r_index == 0 else None

    def code_slide(heading: str, text: str):
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        place_title(slide, heading)
        box = slide.shapes.add_textbox(Inches(0.7), Inches(1.7), Inches(11.9), Inches(5.2))
        box.text_frame.word_wrap = True
        box.text_frame.text = text[:1800]
        for paragraph in box.text_frame.paragraphs:
            for run in paragraph.runs:
                run.font.name, run.font.size = "Consolas", Pt(14)

    # Title slide: the first H1 and the first paragraph after it.
    index = 0
    deck_title, subtitle = title, ""
    if blocks and blocks[0].kind == "heading" and blocks[0].level == 1:
        deck_title = _plain(blocks[0].runs).strip() or title
        index = 1
        if index < len(blocks) and blocks[index].kind == "paragraph":
            subtitle = _plain(blocks[index].runs).strip()
            index += 1
    cover = presentation.slides.add_slide(presentation.slide_layouts[0])
    cover.shapes.title.left, cover.shapes.title.top = Inches(0.9), Inches(2.2)
    cover.shapes.title.width, cover.shapes.title.height = Inches(11.5), Inches(1.6)
    cover.shapes.title.text_frame.text = deck_title
    for paragraph in cover.shapes.title.text_frame.paragraphs:
        for run in paragraph.runs:
            run.font.size, run.font.bold, run.font.color.rgb = Pt(40), True, accent
    sub = cover.placeholders[1]
    sub.left, sub.top, sub.width, sub.height = Inches(0.9), Inches(4.0), Inches(11.5), Inches(1.6)
    sub.text_frame.text = subtitle[:200]

    # Remaining blocks grouped into slides by heading.
    groups: List[Tuple[str, List[Block]]] = []
    current_title, current = deck_title, []
    for block in blocks[index:]:
        if block.kind == "heading" and block.level <= 2:
            if current:
                groups.append((current_title, current))
            current_title, current = _plain(block.runs).strip() or deck_title, []
        elif block.kind != "rule":
            current.append(block)
    if current:
        groups.append((current_title, current))

    for heading, items in groups:
        text_items: List[Block] = []
        parts = [0]  # slides made for this heading so far; later ones are marked "(cont.)"

        def label() -> str:
            return heading + (" (cont.)" if parts[0] else "")

        def emit_text():
            if text_items:
                bullet_slide(label(), list(text_items))
                text_items.clear()
                parts[0] += 1

        for item in items:
            if item.kind == "table":
                emit_text()
                table_slide(label(), item)
                parts[0] += 1
            elif item.kind == "code":
                emit_text()
                code_slide(label(), item.text)
                parts[0] += 1
            else:
                text_items.append(item)
                if len(text_items) >= 7 or sum(len(_plain(i.runs)) for i in text_items) > 800:
                    emit_text()
        emit_text()
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def render(markdown: str, fmt: str, title: str) -> bytes:
    if fmt == "docx":
        return render_docx(markdown, title)
    if fmt == "pdf":
        return render_pdf(markdown, title)
    if fmt == "xlsx":
        return render_xlsx(markdown, title)
    if fmt == "pptx":
        return render_pptx(markdown, title)
    if fmt == "csv":
        return render_csv(markdown)
    if fmt == "txt":
        return render_txt(markdown)
    if fmt == "md":
        return render_markdown(markdown)
    if fmt == "html":
        return render_html(markdown, title)
    raise DocumentGenerationError(f"Unsupported output format '{fmt}'.")


# ---------------------------------------------------------------------------------------------------------
# Storage: files belong to the user who asked for them and expire
# ---------------------------------------------------------------------------------------------------------

@dataclass
class GeneratedFile:
    id: str
    filename: str
    label: str
    fmt: str
    size: int


def _directory() -> Path:
    path = Path(settings.GENERATED_FILES_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


_last_purge = 0.0


def purge_expired(force: bool = False) -> int:
    """Delete generated files older than GENERATED_FILE_TTL_DAYS (checked at most every 10 minutes)."""
    global _last_purge
    now = time.time()
    if not force and now - _last_purge < 600:
        return 0
    _last_purge = now
    removed = 0
    cutoff = now - max(1, int(settings.GENERATED_FILE_TTL_DAYS)) * 86400
    try:
        for item in _directory().iterdir():
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
                removed += 1
    except OSError as exc:
        logger.warning("Purging generated files failed (%s)", type(exc).__name__)
    return removed


def store_file(owner_id: Optional[str], data: bytes, filename: str, fmt: str) -> GeneratedFile:
    if len(data) > MAX_GENERATED_BYTES:
        raise DocumentGenerationError("The generated file is too large.")
    purge_expired()
    file_id = uuid.uuid4().hex
    extension, media_type, label = FORMATS[fmt]
    folder = _directory()
    (folder / f"{file_id}.bin").write_bytes(data)
    (folder / f"{file_id}.json").write_text(
        json.dumps({"owner": str(owner_id), "filename": filename, "media_type": media_type, "created": time.time()}),
        encoding="utf-8",
    )
    return GeneratedFile(id=file_id, filename=filename, label=label, fmt=fmt, size=len(data))


def load_file(owner_id: str, file_id: str) -> Optional[Tuple[Path, Dict[str, Any]]]:
    """(path, metadata) when the file exists, has not expired and belongs to `owner_id`; otherwise None."""
    if not _FILE_ID.match(file_id or ""):
        return None
    folder = _directory()
    meta_path, data_path = folder / f"{file_id}.json", folder / f"{file_id}.bin"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("owner") != str(owner_id) or not data_path.is_file():
            return None
        if time.time() - float(meta.get("created", 0)) > max(1, int(settings.GENERATED_FILE_TTL_DAYS)) * 86400:
            return None
        return data_path, meta
    except (OSError, ValueError):
        return None


LINK_PREFIX = "/generated-files/"  # relative, so Markdown keeps the link; the chat turns it into a download button


def create_documents(owner_id: Optional[str], markdown: str, formats: List[str], request_title: str = "") -> Tuple[str, List[GeneratedFile]]:
    """
    Build the requested files from `markdown`. Returns (the chat message with download links, the files). If no
    file could be created the message is the content itself plus a note, so nothing the model wrote is lost.
    """
    content = _clean_markdown(markdown)
    if not content:
        raise DocumentGenerationError("There was no content to put into a document.")
    title = title_of(content, request_title.strip()[:80] or "Document")
    outline = _outline(parse_markdown(content))
    files: List[GeneratedFile] = []
    for fmt in normalize_formats(formats):
        try:
            data = render(content, fmt, title)
            files.append(store_file(owner_id, data, _safe_filename(title, FORMATS[fmt][0]), fmt))
        except Exception as exc:
            logger.warning("Creating a %s file failed (%s)", fmt, type(exc).__name__, exc_info=True)
    if not files:
        return f"{content}\n\n> I couldn't create the file, so the content is shown here instead.", []
    lines = [f"I've created **{title}** for you."]
    lines += [f"[{f.filename}]({LINK_PREFIX}{f.id})" for f in files]
    if outline:
        lines.append("**Includes:** " + ", ".join(outline))
    return "\n\n".join(lines), files
