"""Any-file reading (PowerPoint, Excel, OpenDocument, HTML, RTF, ZIP, code/text) and linked-page reading with
SSRF protection. Office files are built in memory from their XML, no network, no model."""

import io
import socket
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.api import chats as chats_api
from backend.app.main import app
from backend.app.schemas.message import MAX_ATTACHMENTS, MessageCreate
from backend.app.security.auth import get_current_user
from backend.app.services import url_reader
from backend.app.services.document_extractor import extract_document

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _zip(files: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _slide(*paragraphs):
    body = "".join(f"<a:p><a:r><a:t>{p}</a:t></a:r></a:p>" for p in paragraphs)
    return f'<p:sld xmlns:a="{A}" xmlns:p="p"><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>'


def _pptx():
    return _zip({
        "ppt/slides/slide1.xml": _slide("SAP BRIM Overview", "Convergent Invoicing bills usage"),
        "ppt/slides/slide2.xml": _slide("Rating", "CC rates events"),
        "ppt/notesSlides/notesSlide2.xml": _slide("Remember the rating plan", "2"),
    })


def _xlsx():
    shared = f'<sst xmlns="{S}"><si><t>Item</t></si><si><t>Billable item</t></si><si><t>Qty</t></si></sst>'
    sheet = (f'<worksheet xmlns="{S}"><sheetData>'
             '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>2</v></c></row>'
             '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>7</v></c><c r="C2"><v>42</v></c></row>'
             '</sheetData></worksheet>')
    workbook = f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Usage" sheetId="1" r:id="rId1"/></sheets></workbook>'
    rels = '<Relationships xmlns="r"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'
    return _zip({"xl/sharedStrings.xml": shared, "xl/worksheets/sheet1.xml": sheet,
                 "xl/workbook.xml": workbook, "xl/_rels/workbook.xml.rels": rels})


# ---- readers ------------------------------------------------------------------------------------------

def test_pptx_slides_and_speaker_notes_are_read():
    result = extract_document("deck.pptx", _pptx())
    assert result["file_type"] == "pptx" and result["page_count"] == 2
    assert "--- Slide 1 ---" in result["text"] and "Convergent Invoicing bills usage" in result["text"]
    assert "[Speaker notes]" in result["text"] and "Remember the rating plan" in result["text"]
    assert "\n2\n" not in result["text"]  # the slide-number placeholder is not content


def test_xlsx_sheets_rows_and_shared_strings_are_read():
    result = extract_document("usage.xlsx", _xlsx())
    assert "--- Sheet: Usage ---" in result["text"]
    assert "Item | | Qty" in result["text"] and "Billable item | 7 | 42" in result["text"]


def test_opendocument_html_rtf_and_code_files_are_read():
    odt = _zip({"content.xml": '<office:document-content xmlns:office="o" xmlns:text="t"><text:p>Hello ODT</text:p></office:document-content>'})
    assert "Hello ODT" in extract_document("a.odt", odt)["text"]
    page = b"<html><head><title>BRIM Guide</title><style>x{}</style></head><body><nav>menu</nav><main><h1>Billing</h1><p>Text here</p><script>bad()</script></main></body></html>"
    html = extract_document("page.html", page)["text"]
    assert "BRIM Guide" in html and "# Billing" in html and "Text here" in html and "bad()" not in html and "menu" not in html
    assert "Hello world" in extract_document("n.rtf", rb"{\rtf1\ansi Hello world\par}")["text"]
    abap = extract_document("zreport.abap", b"REPORT zbrim.\nWRITE 'hi'.")
    assert abap["file_type"] == "abap" and "REPORT zbrim" in abap["text"]
    assert "key=value" in extract_document("settings.custom", b"key=value\nother=1")["text"]  # unknown extension, text content


def test_zip_of_a_folder_is_read_file_by_file_and_skips_what_it_cannot_read():
    archive = _zip({"docs/a.txt": "alpha text", "docs/deck.pptx": _pptx(), ".git/config": "secret", "bin/tool.exe": b"MZ\x00\x01\x02" * 50})
    result = extract_document("project.zip", archive)
    assert "=== docs/a.txt ===" in result["text"] and "alpha text" in result["text"]
    assert "Convergent Invoicing" in result["text"] and "secret" not in result["text"]
    assert "skipped" in result["text"]


@pytest.mark.parametrize("name,data,message", [
    ("old.ppt", b"anything", "Unsupported file format"),
    ("tool.exe", b"MZ\x00\x00\x01" * 100, "Unsupported file format"),
    ("broken.pptx", b"not a zip", "not a valid PowerPoint"),
])
def test_unreadable_files_are_refused_with_a_reason(name, data, message):
    with pytest.raises(ValueError, match=message):
        extract_document(name, data)


def test_zip_bomb_is_refused_before_unpacking():
    bomb = _zip({"big.txt": "A" * (61 * 1024 * 1024)})
    with pytest.raises(ValueError, match="too large"):
        extract_document("bomb.pptx", bomb)


# ---- endpoints ----------------------------------------------------------------------------------------

@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u1", is_admin=False)
    yield TestClient(app)


def test_extract_document_endpoint_accepts_any_readable_type(client):
    response = client.post("/api/chats/extract-document", files={"file": ("deck.pptx", _pptx(), "application/octet-stream")})
    assert response.status_code == 200 and "Convergent Invoicing" in response.json()["text"]
    response = client.post("/api/chats/extract-document", files={"file": ("model.xyz", b"\x00\x01\x02" * 100, "application/octet-stream")})
    assert response.status_code == 400 and "Unsupported file format" in response.json()["detail"]


def test_attachment_limits_allow_a_folder_but_not_unbounded_text():
    item = {"name": "a.txt", "file_type": "txt", "text": "x"}
    assert MessageCreate(content="q", attachments=[item] * MAX_ATTACHMENTS)
    with pytest.raises(ValueError):
        MessageCreate(content="q", attachments=[item] * (MAX_ATTACHMENTS + 1))
    with pytest.raises(ValueError):
        MessageCreate(content="q", attachments=[{**item, "text": "x" * 150000}] * 11)


# ---- links: safety --------------------------------------------------------------------------------------

def _resolves_to(monkeypatch, *addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **k: [(2, 1, 6, "", (a, port)) for a in addresses])


@pytest.mark.parametrize("url", [
    "ftp://example.com/x", "file:///etc/passwd", "http://user:pw@example.com/", "https://example.com:22/", "javascript:alert(1)", "",
])
def test_only_plain_http_links_without_credentials_on_web_ports(monkeypatch, url):
    _resolves_to(monkeypatch, "93.184.216.34")
    with pytest.raises(url_reader.UrlReadError):
        url_reader.validate_url(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "172.16.0.1", "169.254.169.254", "::1", "fe80::1", "0.0.0.0", "::ffff:127.0.0.1", "100.64.0.1"])
def test_private_loopback_and_metadata_addresses_are_blocked(monkeypatch, address):
    _resolves_to(monkeypatch, address)
    with pytest.raises(url_reader.UrlReadError, match="private or internal"):
        url_reader.validate_url("https://intranet.example.com/")


def test_a_host_with_any_private_address_is_blocked_and_public_ones_are_pinned(monkeypatch):
    _resolves_to(monkeypatch, "93.184.216.34", "10.0.0.1")
    with pytest.raises(url_reader.UrlReadError):
        url_reader.validate_url("https://mixed.example.com/")
    _resolves_to(monkeypatch, "93.184.216.34")
    assert url_reader.validate_url("https://example.com/a")[3] == "93.184.216.34"


def test_redirect_to_an_internal_address_is_blocked(monkeypatch):
    addresses = {"public.example.com": "93.184.216.34", "internal.example.com": "10.1.2.3"}
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **k: [(2, 1, 6, "", (addresses[host], port))])
    calls = []

    class FakeResponse:
        status_code = 302
        headers = {"location": "http://internal.example.com/admin"}

        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeClient:
        def __init__(self, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url, **k):
            calls.append(url)
            return FakeResponse()

    monkeypatch.setattr(url_reader.httpx, "Client", FakeClient)
    with pytest.raises(url_reader.UrlReadError, match="private or internal"):
        url_reader.read_url("http://public.example.com/start")
    assert calls == ["http://93.184.216.34/start"]  # connected to the checked IP; the internal host was never contacted


def test_urls_are_found_in_a_message_without_trailing_punctuation():
    text = "See https://example.com/a/b?x=1. Also (http://foo.org/page), and https://example.com/a/b?x=1 again"
    assert url_reader.extract_urls(text) == ["https://example.com/a/b?x=1", "http://foo.org/page"]
    assert url_reader.extract_urls("no links here") == []


# ---- links: reading ---------------------------------------------------------------------------------------

def _serve(monkeypatch, content_type, body, disposition=None):
    monkeypatch.setattr(url_reader, "_fetch", lambda url: (url, content_type, body, disposition))


def test_a_linked_web_page_is_read_with_its_title_and_source(monkeypatch):
    _serve(monkeypatch, "text/html", b"<html><head><title>SAP Help: Billing</title></head><body><p>Billable items are created from consumption.</p></body></html>")
    result = url_reader.read_url("https://help.sap.com/doc/billing")
    assert result["file_type"] == "url" and result["filename"] == "SAP Help: Billing"
    assert "Source URL: https://help.sap.com/doc/billing" in result["text"] and "Billable items are created" in result["text"]


def test_a_linked_pptx_or_pdf_file_goes_through_the_same_readers(monkeypatch):
    _serve(monkeypatch, "application/vnd.openxmlformats-officedocument.presentationml.presentation", _pptx())
    assert "Convergent Invoicing" in url_reader.read_url("https://files.example.com/deck")["text"]
    _serve(monkeypatch, "application/octet-stream", _pptx(), disposition="slides.pptx")
    assert "Rating" in url_reader.read_url("https://files.example.com/download?id=3")["text"]


def test_a_link_with_no_readable_text_gives_a_clear_error(monkeypatch):
    _serve(monkeypatch, "text/html", b"<html><body><script>app()</script></body></html>")
    with pytest.raises(url_reader.UrlReadError, match="No readable text"):
        url_reader.read_url("https://spa.example.com/")


def test_extract_url_endpoint(client, monkeypatch):
    monkeypatch.setattr(url_reader, "_fetch", lambda url: (url, "text/plain", b"plain page text", None))
    monkeypatch.setattr(url_reader, "validate_url", lambda url: (url, "example.com", 443, "93.184.216.34"))
    ok = client.post("/api/chats/extract-url", json={"url": "https://example.com/notes.txt"})
    assert ok.status_code == 200 and "plain page text" in ok.json()["text"] and ok.json()["file_type"] == "url"
    monkeypatch.setattr(url_reader, "_fetch", lambda url: (_ for _ in ()).throw(url_reader.UrlReadError("blocked reason")))
    bad = client.post("/api/chats/extract-url", json={"url": "http://10.0.0.1/"})
    assert bad.status_code == 400 and bad.json()["detail"] == "blocked reason"
    assert client.post("/api/chats/extract-url", json={"url": "x"}).status_code == 422
