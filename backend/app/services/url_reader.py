"""
Reads a web page or online file a user links to, so a chat question can be answered from it.

The server fetches the URL on the user's behalf, so this is the classic SSRF surface and is locked down:
  * http/https only, no credentials in the URL, ports 80/443/8080/8443 only;
  * the host must resolve ONLY to public addresses (no loopback, private, link-local, cloud-metadata or
    reserved ranges), checked again on every redirect (at most 5, followed manually);
  * the connection is pinned to the address that was checked (no second DNS lookup between check and
    connect, so DNS rebinding cannot swap it), with the original host kept for TLS and the Host header;
  * hard caps on size (10 MB), time (15 s) and redirects. Cookies and credentials are never sent.
The fetched content goes through the same extractors as uploads and is treated as untrusted data.
"""

import ipaddress
import logging
import mimetypes
import re
import socket
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from backend.app.services.document_extractor import (
    TRUNCATION_MARKER,
    MAX_CHARACTERS_PER_DOCUMENT,
    clean_extracted_text,
    extract_document,
    html_to_text,
)

logger = logging.getLogger(__name__)

MAX_URL_BYTES = 10 * 1024 * 1024
URL_TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 5
ALLOWED_PORTS = {80, 443, 8080, 8443}
USER_AGENT = "Clyptus-LinkReader/1.0 (+reads a page the user linked in a chat)"

URL_PATTERN = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)

# Content types that are not a file extension in the URL.
_TYPE_EXTENSIONS = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
    "text/csv": "csv",
    "application/json": "json",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/plain": "txt",
    "text/markdown": "md",
    "application/zip": "zip",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/gif": "gif",
}


class UrlReadError(ValueError):
    """The link cannot be read; the message is safe to show to the user."""


def extract_urls(text: str, limit: int = 3) -> List[str]:
    """http(s) links in a message, in order, without trailing punctuation and without duplicates."""
    found: List[str] = []
    for match in URL_PATTERN.findall(text or ""):
        url = match.rstrip(".,;:!?)]}>'\"")
        if url not in found:
            found.append(url)
        if len(found) >= limit:
            break
    return found


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _resolve_public(host: str, port: int) -> str:
    """The IP to connect to: the first address of `host`, only if EVERY address it resolves to is public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UrlReadError("The link's address could not be found.") from exc
    addresses = [info[4][0] for info in infos]
    if not addresses or not all(_is_public(a) for a in addresses):
        raise UrlReadError("That link points to a private or internal address, so it cannot be opened.")
    return addresses[0]


def validate_url(url: str) -> Tuple[str, str, int, str]:
    """(normalised url, host, port, pinned IP) for a link the server may open; UrlReadError otherwise."""
    url = (url or "").strip()
    if not url or len(url) > 2000:
        raise UrlReadError("Enter a valid web link (http or https).")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise UrlReadError("Only http and https links can be read.")
    if parts.username or parts.password:
        raise UrlReadError("Links that contain a username or password cannot be read.")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UrlReadError("That link is not valid.") from exc
    if port not in ALLOWED_PORTS:
        raise UrlReadError("That link uses a port that cannot be opened.")
    host = parts.hostname.lower()
    return url, host, port, _resolve_public(host, port)


def _fetch(url: str) -> Tuple[str, str, bytes, Optional[str]]:
    """(final url, content type, body, content-disposition filename). Follows redirects itself, re-validating each."""
    for _ in range(MAX_REDIRECTS + 1):
        url, host, port, ip = validate_url(url)
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        netloc_ip = f"[{ip}]" if ":" in ip else ip
        default_port = 443 if scheme == "https" else 80
        pinned = parts._replace(netloc=netloc_ip if port == default_port else f"{netloc_ip}:{port}").geturl()
        headers = {
            "Host": host if port == default_port else f"{host}:{port}",
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain,*/*;q=0.5",
            "Accept-Encoding": "gzip, deflate",
        }
        try:
            with httpx.Client(timeout=URL_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
                with client.stream("GET", pinned, headers=headers, extensions={"sni_hostname": host}) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise UrlReadError("The link redirected without a destination.")
                        url = urljoin(url, location)
                        continue
                    if response.status_code >= 400:
                        raise UrlReadError(f"The website answered with an error ({response.status_code}), so the page could not be read.")
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > MAX_URL_BYTES:
                        raise UrlReadError("That page or file is larger than the 10MB limit.")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_URL_BYTES:
                            raise UrlReadError("That page or file is larger than the 10MB limit.")
                    disposition = response.headers.get("content-disposition", "")
                    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.IGNORECASE)
                    return (
                        url,
                        response.headers.get("content-type", "").split(";")[0].strip().lower(),
                        bytes(body),
                        unquote(match.group(1)) if match else None,
                    )
        except UrlReadError:
            raise
        except httpx.TimeoutException as exc:
            raise UrlReadError("The website took too long to answer.") from exc
        except httpx.HTTPError as exc:
            logger.info("URL fetch failed (%s)", type(exc).__name__)
            raise UrlReadError("The website could not be reached.") from exc
    raise UrlReadError("The link redirected too many times.")


def _filename_for(url: str, content_type: str, disposition_name: Optional[str]) -> str:
    name = disposition_name or unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = re.sub(r"[\\/:*?\"<>|]+", "-", name).strip(" .-")
    ext = _TYPE_EXTENSIONS.get(content_type) or (mimetypes.guess_extension(content_type) or "").lstrip(".")
    if "." not in name and ext:
        name = f"{name or 'page'}.{ext}"
    return name or "page.html"


def read_url(url: str) -> Dict[str, Any]:
    """The extraction result (same shape as an uploaded file) for a web page or online file."""
    final_url, content_type, body, disposition_name = _fetch(url)
    if not body:
        raise UrlReadError("The link returned no content.")
    host = urlsplit(final_url).hostname or "link"
    is_page = content_type in ("text/html", "application/xhtml+xml", "") or (
        content_type not in _TYPE_EXTENSIONS and content_type.startswith("text/html")
    )
    try:
        if is_page:
            html = body.decode("utf-8", errors="replace")
            title, text = html_to_text(html)
            display = title or host
            extracted: Dict[str, Any] = {"page_count": 1}
        else:
            filename = _filename_for(final_url, content_type, disposition_name)
            extracted = extract_document(filename, body)
            text, display = extracted["text"], filename
    except UrlReadError:
        raise
    except ValueError as exc:
        raise UrlReadError(str(exc)) from exc
    text = clean_extracted_text(text)
    if not text.strip():
        raise UrlReadError("No readable text was found at that link (the page may need JavaScript or a login).")
    text = f"Source URL: {final_url}\nTitle: {display}\n\n{text}"
    if len(text) > MAX_CHARACTERS_PER_DOCUMENT:
        text = text[:MAX_CHARACTERS_PER_DOCUMENT - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    name = re.sub(r"[\\/]+", "-", display).strip()[:200] or host
    return {
        "filename": name,
        "file_type": "url",
        "size_bytes": len(body),
        "page_count": extracted.get("page_count") or 1,
        "char_count": len(text),
        "preview": text[:300].strip() + ("..." if len(text) > 300 else ""),
        "text": text,
        "pages": [],
    }
