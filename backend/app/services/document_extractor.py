import io
import re
import logging
from functools import lru_cache
from typing import Dict, Any, List, Optional
from pathlib import Path
from backend.app.config import settings
from backend.app.security.log_safety import privacy_fingerprint

logger = logging.getLogger(__name__)

MAX_CHARACTERS_PER_DOCUMENT = 150000
MAX_IMAGE_PIXELS = 25_000_000


class PDFPageLimitError(ValueError):
    pass


def clean_extracted_text(text: str) -> str:
    """Cleans raw text by removing non-printable glyphs and normalizing whitespace."""
    if not text:
        return ""
    # Filter private-use unicode glyphs and replacement chars
    text = re.sub(r"[\ue000-\uf8ff\ufffd]", " ", text)
    # Normalize line breaks and tabs
    text = re.sub(r"\r\n|\r", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_from_pdf(filename: str, file_bytes: bytes) -> Dict[str, Any]:
    """Extracts text, pages, and metadata from PDF bytes using PyMuPDF with PyPDF fallback."""
    pages_list: List[Dict[str, Any]] = []
    full_text_chunks: List[str] = []
    page_count = 0

    # 1. Primary extractor: PyMuPDF (fitz)
    try:
        import fitz
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        page_count = len(doc)
        if page_count > settings.MAX_PDF_PAGES:
            doc.close()
            raise PDFPageLimitError(f"PDF exceeds the configured {settings.MAX_PDF_PAGES}-page limit.")

        for page_idx in range(page_count):
            page = doc[page_idx]
            page_num = page_idx + 1
            raw_page_text = page.get_text("text") or ""
            cleaned = clean_extracted_text(raw_page_text)

            pages_list.append({
                "page": page_num,
                "text": cleaned,
                "char_count": len(cleaned)
            })
            if cleaned:
                full_text_chunks.append(f"--- Page {page_num} ---\n{cleaned}")
        doc.close()
    except PDFPageLimitError:
        raise
    except Exception as e_fitz:
        logger.warning("PyMuPDF extraction failed for document_ref=%s (%s); falling back to pypdf.", privacy_fingerprint(filename), type(e_fitz).__name__)
        # 2. Fallback extractor: pypdf
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            page_count = len(reader.pages)
            if page_count > settings.MAX_PDF_PAGES:
                raise PDFPageLimitError(f"PDF exceeds the configured {settings.MAX_PDF_PAGES}-page limit.")
            for page_idx, page in enumerate(reader.pages):
                page_num = page_idx + 1
                raw_text = page.extract_text() or ""
                cleaned = clean_extracted_text(raw_text)
                pages_list.append({
                    "page": page_num,
                    "text": cleaned,
                    "char_count": len(cleaned)
                })
                if cleaned:
                    full_text_chunks.append(f"--- Page {page_num} ---\n{cleaned}")
        except PDFPageLimitError:
            raise
        except Exception as e_pypdf:
            logger.error("pypdf extraction also failed for document_ref=%s (%s)", privacy_fingerprint(filename), type(e_pypdf).__name__)
            raise ValueError(f"Failed to extract text from PDF '{filename}': {e_pypdf}")

    full_text = "\n\n".join(full_text_chunks).strip()
    if len(full_text) > MAX_CHARACTERS_PER_DOCUMENT:
        marker = "\n\n[Document content truncated to fit token limits]"
        full_text = full_text[:MAX_CHARACTERS_PER_DOCUMENT - len(marker)] + marker

    preview = full_text[:300].strip() + ("..." if len(full_text) > 300 else "")

    return {
        "filename": filename,
        "file_type": "pdf",
        "size_bytes": len(file_bytes),
        "page_count": page_count,
        "char_count": len(full_text),
        "preview": preview,
        "text": full_text,
        "pages": pages_list,
    }


def extract_from_docx(filename: str, file_bytes: bytes) -> Dict[str, Any]:
    """Extracts text from Word DOCX bytes including paragraphs, headings, and tables."""
    try:
        import docx
        doc = docx.Document(io.BytesIO(file_bytes))
        paragraphs: List[str] = []

        for p in doc.paragraphs:
            text = clean_extracted_text(p.text)
            if text:
                if p.style and p.style.name.startswith("Heading"):
                    paragraphs.append(f"### {text}")
                else:
                    paragraphs.append(text)

        # Extract table content
        for t_idx, table in enumerate(doc.tables, 1):
            table_rows: List[str] = []
            for row in table.rows:
                row_cells = [clean_extracted_text(cell.text) for cell in row.cells]
                table_rows.append(" | ".join(row_cells))
            if table_rows:
                paragraphs.append(f"\n[Table {t_idx}]\n" + "\n".join(table_rows))

        full_text = "\n\n".join(paragraphs).strip()
        if len(full_text) > MAX_CHARACTERS_PER_DOCUMENT:
            marker = "\n\n[Document content truncated to fit token limits]"
            full_text = full_text[:MAX_CHARACTERS_PER_DOCUMENT - len(marker)] + marker

        preview = full_text[:300].strip() + ("..." if len(full_text) > 300 else "")

        return {
            "filename": filename,
            "file_type": "docx",
            "size_bytes": len(file_bytes),
            "page_count": max(1, len(paragraphs) // 15),
            "char_count": len(full_text),
            "preview": preview,
            "text": full_text,
            "pages": [],
        }
    except Exception as e:
        logger.error("DOCX extraction failed for document_ref=%s (%s)", privacy_fingerprint(filename), type(e).__name__)
        raise ValueError(f"Failed to extract text from DOCX document '{filename}': {e}")


def extract_from_plaintext(filename: str, file_bytes: bytes, file_type: str) -> Dict[str, Any]:
    """Extracts text from plain text, markdown, CSV, JSON, or log files."""
    text = ""
    for encoding in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
        try:
            text = file_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    cleaned = clean_extracted_text(text)
    if len(cleaned) > MAX_CHARACTERS_PER_DOCUMENT:
        marker = "\n\n[Document content truncated to fit token limits]"
        cleaned = cleaned[:MAX_CHARACTERS_PER_DOCUMENT - len(marker)] + marker

    line_count = len(cleaned.splitlines())
    estimated_pages = max(1, line_count // 45)
    preview = cleaned[:300].strip() + ("..." if len(cleaned) > 300 else "")

    return {
        "filename": filename,
        "file_type": file_type,
        "size_bytes": len(file_bytes),
        "page_count": estimated_pages,
        "char_count": len(cleaned),
        "preview": preview,
        "text": cleaned,
        "pages": [],
    }


@lru_cache(maxsize=1)
def _get_ocr_engine():
    """Initialize the local OCR engine on demand and reuse it across uploads."""
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise ValueError(
            "PNG OCR is unavailable. Install the backend OCR dependencies and restart the API."
        ) from exc
    return RapidOCR()


def extract_from_png(filename: str, file_bytes: bytes) -> Dict[str, Any]:
    """Extract text and a simple reading-order description from an uploaded PNG."""
    if not file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("The uploaded file is not a valid PNG image.")
    try:
        from PIL import Image, ImageOps
        import numpy as np

        with Image.open(io.BytesIO(file_bytes)) as source_image:
            if source_image.format != "PNG":
                raise ValueError("The uploaded file is not a valid PNG image.")
            width, height = source_image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("PNG dimensions exceed the supported image size.")
            image = ImageOps.exif_transpose(source_image).convert("RGB")
            ocr_result = _get_ocr_engine()(np.asarray(image))

        text_blocks = []
        boxes = getattr(ocr_result, "boxes", None) if ocr_result else None
        recognized = getattr(ocr_result, "txts", None) if ocr_result else None
        for box, detected_text in zip(
            boxes if boxes is not None else [],
            recognized if recognized is not None else [],
        ):
            detected_text = clean_extracted_text(str(detected_text))
            if not detected_text:
                continue
            try:
                top = min(float(point[1]) for point in box)
                left = min(float(point[0]) for point in box)
            except (TypeError, ValueError, IndexError):
                top, left = 0.0, 0.0
            text_blocks.append((top, left, detected_text))

        # OCR boxes retain useful ordering for screenshots, forms, and diagrams.
        text_blocks.sort(key=lambda block: (round(block[0] / 20), block[1]))
        recognized_lines = [block[2] for block in text_blocks]
        if not recognized_lines:
            raise ValueError("No readable text was detected in this PNG image.")

        extracted_text = (
            f"Image: {Path(filename).name} ({width} x {height} pixels)\n"
            "OCR text in approximate top-to-bottom, left-to-right reading order:\n"
            + "\n".join(recognized_lines)
        )
        extracted_text = clean_extracted_text(extracted_text)[:MAX_CHARACTERS_PER_DOCUMENT]
        return {
            "filename": Path(filename).name,
            "file_type": "png",
            "size_bytes": len(file_bytes),
            "page_count": 1,
            "char_count": len(extracted_text),
            "preview": extracted_text[:300] + ("..." if len(extracted_text) > 300 else ""),
            "text": extracted_text,
            "pages": [{"page": 1, "text": extracted_text, "char_count": len(extracted_text)}],
        }
    except ValueError:
        raise
    except Exception as exc:
        logger.warning("PNG OCR failed for document_ref=%s (%s)", privacy_fingerprint(Path(filename).name), type(exc).__name__)
        raise ValueError(f"Could not read text from PNG image '{Path(filename).name}'.") from exc


def extract_from_image(filename: str, file_bytes: bytes, file_type: str = "image") -> Dict[str, Any]:
    """Extract text from JPG, WEBP, BMP, TIFF or general image bytes using OCR."""
    try:
        from PIL import Image, ImageOps
        import numpy as np

        with Image.open(io.BytesIO(file_bytes)) as source_image:
            width, height = source_image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError(f"{file_type.upper()} dimensions exceed the supported image size.")
            image = ImageOps.exif_transpose(source_image).convert("RGB")
            ocr_result = _get_ocr_engine()(np.asarray(image))

        text_blocks = []
        boxes = getattr(ocr_result, "boxes", None) if ocr_result else None
        recognized = getattr(ocr_result, "txts", None) if ocr_result else None
        for box, detected_text in zip(
            boxes if boxes is not None else [],
            recognized if recognized is not None else [],
        ):
            detected_text = clean_extracted_text(str(detected_text))
            if not detected_text:
                continue
            try:
                top = min(float(point[1]) for point in box)
                left = min(float(point[0]) for point in box)
            except (TypeError, ValueError, IndexError):
                top, left = 0.0, 0.0
            text_blocks.append((top, left, detected_text))

        text_blocks.sort(key=lambda block: (round(block[0] / 20), block[1]))
        recognized_lines = [block[2] for block in text_blocks]
        if not recognized_lines:
            raise ValueError(f"No readable text was detected in this {file_type.upper()} image.")

        extracted_text = (
            f"Image: {Path(filename).name} ({width} x {height} pixels)\n"
            "OCR text in approximate reading order:\n"
            + "\n".join(recognized_lines)
        )
        extracted_text = clean_extracted_text(extracted_text)[:MAX_CHARACTERS_PER_DOCUMENT]
        return {
            "filename": Path(filename).name,
            "file_type": file_type,
            "size_bytes": len(file_bytes),
            "page_count": 1,
            "char_count": len(extracted_text),
            "preview": extracted_text[:300] + ("..." if len(extracted_text) > 300 else ""),
            "text": extracted_text,
            "pages": [{"page": 1, "text": extracted_text, "char_count": len(extracted_text)}],
        }
    except ValueError:
        raise
    except Exception as exc:
        logger.warning("Image OCR failed for document_ref=%s (%s)", privacy_fingerprint(Path(filename).name), type(exc).__name__)
        raise ValueError(f"Could not read text from image '{Path(filename).name}': {exc}") from exc


# ---------------------------------------------------------------------------------------------------------
# Office / OpenDocument / HTML / RTF / archives. Parsed directly from their XML, so no extra dependencies;
# every archive is size-checked before anything is read (zip bombs), and XML is parsed without entities.
# ---------------------------------------------------------------------------------------------------------

MAX_ZIP_UNCOMPRESSED_BYTES = 60 * 1024 * 1024
MAX_ZIP_MEMBERS = 5000
MAX_ARCHIVE_FILES_READ = 30
MAX_SHEET_ROWS = 3000
TRUNCATION_MARKER = "\n\n[Document content truncated to fit token limits]"

# Old binary Office formats cannot be read without heavy converters; the message tells the user what to do.
LEGACY_BINARY_OFFICE = {"doc", "ppt", "xls"}

# Read as plain text without sniffing: documents, data, configuration, logs and source code.
TEXT_EXTENSIONS = {
    "txt", "md", "markdown", "rst", "csv", "tsv", "json", "jsonl", "log", "xml", "sql", "yaml", "yml", "toml",
    "ini", "cfg", "conf", "properties", "env", "tex", "srt", "vtt", "diff", "patch",
    "py", "js", "jsx", "ts", "tsx", "java", "kt", "c", "h", "cpp", "hpp", "cs", "go", "rs", "rb", "php", "swift",
    "sh", "bat", "ps1", "r", "scala", "lua", "pl", "abap", "css", "scss", "vue",
}

_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _finish(filename: str, ext: str, file_bytes: bytes, text: str, pages: Optional[List[Dict[str, Any]]] = None,
            page_count: Optional[int] = None) -> Dict[str, Any]:
    """The common extraction result: cleaned, length-capped text with preview and page metadata."""
    text = clean_extracted_text(text)
    if len(text) > MAX_CHARACTERS_PER_DOCUMENT:
        text = text[:MAX_CHARACTERS_PER_DOCUMENT - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return {
        "filename": Path(filename).name,
        "file_type": ext,
        "size_bytes": len(file_bytes),
        "page_count": page_count if page_count is not None else (len(pages) if pages else max(1, len(text.splitlines()) // 45)),
        "char_count": len(text),
        "preview": text[:300].strip() + ("..." if len(text) > 300 else ""),
        "text": text,
        "pages": pages or [],
    }


def _open_zip(file_bytes: bytes, kind: str):
    import zipfile
    try:
        archive = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"This is not a valid {kind} file.") from exc
    members = archive.infolist()
    if len(members) > MAX_ZIP_MEMBERS or sum(m.file_size for m in members) > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise ValueError(f"This {kind} file is too large to read once unpacked.")
    return archive


def _xml(archive, name: str):
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    return etree.fromstring(archive.read(name), parser=parser)


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _number(name: str) -> int:
    match = re.search(r"(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def _paragraphs(root, ns: str = _A_NS) -> List[str]:
    lines = []
    for paragraph in root.iter(f"{ns}p"):
        text = "".join(t.text or "" for t in paragraph.iter(f"{ns}t")).strip()
        if text:
            lines.append(text)
    return lines


def extract_from_pptx(filename: str, file_bytes: bytes) -> Dict[str, Any]:
    """PowerPoint: the text of every slide (titles, text boxes, tables) plus its speaker notes."""
    try:
        archive = _open_zip(file_bytes, "PowerPoint")
        slides = sorted((n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=_number)
        if not slides:
            raise ValueError("No slides were found in this PowerPoint file.")
        blocks, pages = [], []
        for index, name in enumerate(slides, 1):
            lines = _paragraphs(_xml(archive, name))
            notes_name = f"ppt/notesSlides/notesSlide{_number(name)}.xml"
            notes = _paragraphs(_xml(archive, notes_name)) if notes_name in archive.namelist() else []
            notes = [n for n in notes if not n.isdigit()]  # the slide-number placeholder
            body = "\n".join(lines)
            if notes:
                body += "\n[Speaker notes]\n" + "\n".join(notes)
            body = clean_extracted_text(body)
            pages.append({"page": index, "text": body, "char_count": len(body)})
            if body:
                blocks.append(f"--- Slide {index} ---\n{body}")
        return _finish(filename, "pptx", file_bytes, "\n\n".join(blocks), pages, page_count=len(slides))
    except ValueError:
        raise
    except Exception as exc:
        logger.warning("PPTX extraction failed for document_ref=%s (%s)", privacy_fingerprint(filename), type(exc).__name__)
        raise ValueError(f"Failed to extract text from PowerPoint file '{Path(filename).name}'.") from exc


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Za-z]+", reference or "")
    index = 0
    for char in (letters.group(0).upper() if letters else ""):
        index = index * 26 + (ord(char) - 64)
    return max(index - 1, 0)


def extract_from_xlsx(filename: str, file_bytes: bytes, ext: str = "xlsx") -> Dict[str, Any]:
    """Excel: every worksheet as rows of cell values (shared strings resolved)."""
    try:
        archive = _open_zip(file_bytes, "Excel")
        names = set(archive.namelist())
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            shared = ["".join(t.text or "" for t in si.iter(f"{_S_NS}t")) for si in _xml(archive, "xl/sharedStrings.xml").iter(f"{_S_NS}si")]
        targets: Dict[str, str] = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for rel in _xml(archive, "xl/_rels/workbook.xml.rels"):
                target = rel.get("Target", "")
                targets[rel.get("Id", "")] = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
        sheets = []
        if "xl/workbook.xml" in names:
            for sheet in _xml(archive, "xl/workbook.xml").iter(f"{_S_NS}sheet"):
                path = targets.get(sheet.get(f"{_R_NS}id", ""))
                if path in names:
                    sheets.append((sheet.get("name") or f"Sheet{len(sheets) + 1}", path))
        if not sheets:
            sheets = [(f"Sheet{_number(n)}", n) for n in sorted((n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)), key=_number)]
        if not sheets:
            raise ValueError("No worksheets were found in this Excel file.")

        blocks, pages = [], []
        for index, (title, path) in enumerate(sheets, 1):
            rows = []
            for row in _xml(archive, path).iter(f"{_S_NS}row"):
                cells: Dict[int, str] = {}
                for cell in row.iter(f"{_S_NS}c"):
                    kind = cell.get("t")
                    value_node = cell.find(f"{_S_NS}v")
                    if kind == "inlineStr":
                        value = "".join(t.text or "" for t in cell.iter(f"{_S_NS}t"))
                    elif value_node is None or value_node.text is None:
                        value = ""
                    elif kind == "s":
                        try:
                            value = shared[int(value_node.text)]
                        except (ValueError, IndexError):
                            value = ""
                    elif kind == "b":
                        value = "TRUE" if value_node.text == "1" else "FALSE"
                    else:
                        value = value_node.text
                    if value.strip():
                        cells[_column_index(cell.get("r", ""))] = value.strip()
                if cells:
                    rows.append(" | ".join(cells.get(i, "") for i in range(max(cells) + 1)))
                if len(rows) >= MAX_SHEET_ROWS:
                    rows.append(f"[Sheet truncated after {MAX_SHEET_ROWS} rows]")
                    break
            body = "\n".join(rows)
            pages.append({"page": index, "text": body, "char_count": len(body)})
            if body:
                blocks.append(f"--- Sheet: {title} ---\n{body}")
        return _finish(filename, ext, file_bytes, "\n\n".join(blocks), pages, page_count=len(sheets))
    except ValueError:
        raise
    except Exception as exc:
        logger.warning("XLSX extraction failed for document_ref=%s (%s)", privacy_fingerprint(filename), type(exc).__name__)
        raise ValueError(f"Failed to extract text from Excel file '{Path(filename).name}'.") from exc


def extract_from_opendocument(filename: str, file_bytes: bytes, ext: str) -> Dict[str, Any]:
    """OpenDocument text, presentation and spreadsheet files (.odt, .odp, .ods)."""
    try:
        archive = _open_zip(file_bytes, "OpenDocument")
        if "content.xml" not in archive.namelist():
            raise ValueError("This OpenDocument file has no readable content.")
        root = _xml(archive, "content.xml")
        lines: List[str] = []
        if ext == "ods":
            for row in root.iter():
                if _local(row.tag) == "table-row":
                    cells = ["".join(c.itertext()).strip() for c in row if _local(c.tag) == "table-cell"]
                    if any(cells):
                        lines.append(" | ".join(cells))
        else:
            for element in root.iter():
                if _local(element.tag) in ("h", "p"):
                    text = "".join(element.itertext()).strip()
                    if text:
                        lines.append(text)
        return _finish(filename, ext, file_bytes, "\n".join(lines))
    except ValueError:
        raise
    except Exception as exc:
        logger.warning("OpenDocument extraction failed for document_ref=%s (%s)", privacy_fingerprint(filename), type(exc).__name__)
        raise ValueError(f"Failed to extract text from '{Path(filename).name}'.") from exc


def html_to_text(html: str) -> tuple:
    """(title, readable text) of an HTML page: scripts, styles and, when a main article exists, page chrome dropped."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "lxml")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:300]
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "template", "canvas"]):
        tag.decompose()
    container = soup.find("main") or soup.find("article")
    if container is not None:
        for tag in soup(["nav", "footer", "aside", "header"]):
            if container not in tag.parents:
                tag.decompose()
        root = container
    else:
        root = soup.body or soup
    lines = []
    for block in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "tr", "dt", "dd", "blockquote", "figcaption"]):
        if block.name == "tr":
            text = " | ".join(c.get_text(" ", strip=True) for c in block.find_all(["th", "td"]))
        else:
            if block.find(["p", "li", "pre", "tr"]):  # its children are listed on their own
                continue
            text = block.get_text(" ", strip=True)
        if not text.strip(" |"):
            continue
        lines.append(f"{'#' * int(block.name[1])} {text}" if re.fullmatch(r"h[1-6]", block.name) else text)
    if not lines:  # no block structure at all
        lines = [root.get_text("\n", strip=True)]
    return title, "\n".join(lines)


def extract_from_html(filename: str, file_bytes: bytes, ext: str = "html") -> Dict[str, Any]:
    html = ""
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            html = file_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    title, text = html_to_text(html)
    return _finish(filename, ext, file_bytes, (f"{title}\n\n" if title else "") + text)


def extract_from_rtf(filename: str, file_bytes: bytes) -> Dict[str, Any]:
    raw = file_bytes.decode("latin-1", errors="ignore")
    raw = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes.fromhex(m.group(1)).decode("cp1252", errors="ignore"), raw)
    raw = re.sub(r"\\(par|line|row)\b", "\n", raw)
    raw = re.sub(r"\\tab\b", "\t", raw)
    raw = re.sub(r"\{\\\*[^{}]*\}", "", raw)
    raw = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", raw)
    raw = re.sub(r"[{}\\]", "", raw)
    return _finish(filename, "rtf", file_bytes, raw)


def extract_from_zip(filename: str, file_bytes: bytes) -> Dict[str, Any]:
    """A .zip (for example a zipped folder): each readable file inside is extracted and labelled by its path."""
    archive = _open_zip(file_bytes, "ZIP")
    blocks, read, skipped = [], 0, 0
    for member in sorted(archive.infolist(), key=lambda m: m.filename):
        path = member.filename
        parts = path.split("/")
        if (member.is_dir() or any(p.startswith(".") or p in ("__MACOSX", "node_modules") for p in parts)
                or member.file_size > 10 * 1024 * 1024 or Path(path).suffix.lower() == ".zip"):
            skipped += 1
            continue
        if read >= MAX_ARCHIVE_FILES_READ:
            skipped += 1
            continue
        try:
            part = extract_document(path, archive.read(member))
        except Exception:
            skipped += 1
            continue
        if part.get("text", "").strip():
            blocks.append(f"=== {path} ===\n{part['text']}")
            read += 1
        else:
            skipped += 1
    if not blocks:
        raise ValueError(f"No readable files were found inside '{Path(filename).name}'.")
    if skipped:
        blocks.append(f"[{skipped} other file(s) in the archive were skipped: unsupported, hidden, too large or over the {MAX_ARCHIVE_FILES_READ}-file limit]")
    return _finish(filename, "zip", file_bytes, "\n\n".join(blocks), page_count=read)


def _looks_like_text(file_bytes: bytes) -> bool:
    sample = file_bytes[:8192]
    if not sample or b"\x00" in sample:
        return False
    odd = sum(1 for byte in sample if byte < 9 or 13 < byte < 32)
    return odd / len(sample) < 0.05


def extract_document(filename: str, file_bytes: bytes) -> Dict[str, Any]:
    """
    Main extraction dispatcher. Reads PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx/.xlsm), OpenDocument,
    HTML, RTF, images (OCR), ZIP archives, and any plain-text-like file (data, configuration, logs, source code).
    """
    ext = Path(filename).suffix.lower().lstrip(".")
    if not ext:
        ext = "txt"

    if ext == "pdf":
        return extract_from_pdf(filename, file_bytes)
    elif ext == "png":
        return extract_from_png(filename, file_bytes)
    elif ext in ("jpg", "jpeg", "webp", "bmp", "tiff", "tif", "gif"):
        return extract_from_image(filename, file_bytes, ext)
    elif ext == "docx":
        return extract_from_docx(filename, file_bytes)
    elif ext in ("pptx", "pptm", "ppsx"):
        return extract_from_pptx(filename, file_bytes)
    elif ext in ("xlsx", "xlsm"):
        return extract_from_xlsx(filename, file_bytes, ext)
    elif ext in ("odt", "odp", "ods"):
        return extract_from_opendocument(filename, file_bytes, ext)
    elif ext in ("html", "htm", "xhtml"):
        return extract_from_html(filename, file_bytes, ext)
    elif ext == "rtf":
        return extract_from_rtf(filename, file_bytes)
    elif ext == "zip":
        return extract_from_zip(filename, file_bytes)
    elif ext in LEGACY_BINARY_OFFICE:
        raise ValueError(
            f"Unsupported file format '.{ext}'. Old binary Office files cannot be read; "
            f"open it and save it as .{ext}x (the current format), then upload it again."
        )
    elif ext in TEXT_EXTENSIONS or _looks_like_text(file_bytes):
        return extract_from_plaintext(filename, file_bytes, ext)
    else:
        raise ValueError(
            f"Unsupported file format '.{ext}'. Readable formats: PDF, Word, PowerPoint, Excel, OpenDocument, HTML, RTF, "
            "images, ZIP archives, and text, data, configuration or source-code files."
        )
