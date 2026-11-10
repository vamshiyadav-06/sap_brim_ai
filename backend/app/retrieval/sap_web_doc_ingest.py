"""
Ingestion, Extraction, and Chunking Engine for Extended Official SAP Documentation and Community Sources.

Complies with non-negotiable architectural requirements:
1. Treat GitHub organization URLs and portal homepages as discovery sources, NOT documents to ingest wholesale.
2. For official SAP documentation, retrieve relevant pages and linked documentation preserving canonical URLs,
   product/version metadata, document headings, and page-level citations.
3. For SAP Community, ingest only relevant technical discussions and articles, preserving the original author,
   publication date when available, canonical URL, and source type.
4. Deduplicate repeated URLs and overlapping content before indexing.
5. Keep official SAP documentation, SAP Community content, and GitHub repositories distinguishable in metadata
   and source-ranking logic.
6. Respect applicable licenses, access restrictions, and content-use policies; do not bypass authentication.
7. Keep all new retrieval features disabled behind feature flags until explicitly approved.
"""

import os
import re
import json
import hashlib
import logging
import urllib.parse
import urllib.request
import urllib.error
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Any, List, Set, Tuple, Optional
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.retrieval.source_registry import (
    source_registry,
    RegisteredSourceEntry,
    normalize_canonical_url
)
from backend.app.models.web_doc_chunk import WebDocChunk

logger = logging.getLogger("sap_web_doc_ingest")

SNAPSHOTS_DIR = getattr(
    settings,
    "WEB_DOC_SNAPSHOTS_DIR",
    Path(__file__).resolve().parent.parent.parent / "data" / "web_doc_snapshots"
)


class StructuredTextHTMLParser(HTMLParser):
    """
    Lightweight, dependency-free HTML extractor.
    Extracts structured document sections, headings (H1, H2, H3), paragraphs,
    and metadata tags while skipping scripts, styles, and navigation chrome.
    """

    def __init__(self):
        super().__init__()
        self.title: str = ""
        self.meta_author: Optional[str] = None
        self.meta_date: Optional[str] = None
        self.meta_description: Optional[str] = None
        self.sections: List[Dict[str, str]] = []  # List of {"heading": str, "text": str}
        
        self._current_heading: str = "Overview"
        self._current_text: List[str] = []
        self._in_title: bool = False
        self._ignore_tags: Set[str] = {"script", "style", "noscript", "nav", "footer", "header", "svg"}
        self._ignore_depth: int = 0
        self._in_heading: bool = False
        self._heading_buffer: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        tag_lower = tag.lower()
        if tag_lower in self._ignore_tags:
            self._ignore_depth += 1
            return
        if self._ignore_depth > 0:
            return

        attr_dict = {k.lower(): (v or "") for k, v in attrs}

        if tag_lower == "title":
            self._in_title = True
        elif tag_lower == "meta":
            name = attr_dict.get("name", "").lower() or attr_dict.get("property", "").lower()
            content = attr_dict.get("content", "").strip()
            if "author" in name and content:
                self.meta_author = content
            elif any(d in name for d in ["date", "published_time", "release"]) and content:
                self.meta_date = content
            elif "description" in name and content:
                self.meta_description = content
        elif tag_lower in {"h1", "h2", "h3", "h4"}:
            self._flush_current_section()
            self._in_heading = True
            self._heading_buffer = []

    def handle_endtag(self, tag: str):
        tag_lower = tag.lower()
        if tag_lower in self._ignore_tags:
            self._ignore_depth = max(0, self._ignore_depth - 1)
            return
        if self._ignore_depth > 0:
            return

        if tag_lower == "title":
            self._in_title = False
        elif tag_lower in {"h1", "h2", "h3", "h4"} and self._in_heading:
            self._in_heading = False
            heading_text = " ".join("".join(self._heading_buffer).split()).strip()
            if heading_text:
                self._current_heading = heading_text
            self._heading_buffer = []
        elif tag_lower in {"p", "div", "li", "tr"}:
            self._current_text.append("\n")

    def handle_data(self, data: str):
        if self._ignore_depth > 0:
            return
        if self._in_title:
            self.title += data.strip() + " "
        elif self._in_heading:
            self._heading_buffer.append(data)
        else:
            cleaned = data.strip()
            if cleaned:
                self._current_text.append(data)

    def _flush_current_section(self):
        text = " ".join("".join(self._current_text).split()).strip()
        if text and len(text) > 40:
            self.sections.append({
                "heading": self._current_heading,
                "text": text
            })
        self._current_text = []

    def close(self):
        super().close()
        self._flush_current_section()
        self.title = " ".join(self.title.split()).strip()


class SAPWebDocIngestService:
    """
    Ingests and processes registered Official SAP Developer, Help, and Community sources.
    Enforces deduplication, discovery restrictions, authority labeling, and citation preservation.
    """

    def __init__(self):
        self.registry = source_registry
        self.snapshots_dir = SNAPSHOTS_DIR
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._seen_canonical_urls: Set[str] = set()
        self._seen_content_hashes: Set[str] = set()

    def fetch_url_content(self, url: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        """
        Fetches web documentation page respecting access controls and HTTP policies.
        Returns: (html_text, status_code, error_message)
        """
        # Only http(s): urlopen would also read file: and custom schemes (Bandit B310).
        if urllib.parse.urlparse(url).scheme.lower() not in ("http", "https"):
            return None, None, "Only http and https URLs can be fetched."
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "cClyptus-SAP-BRIM-Docs-Fetcher/1.0 (+https://clyptusap.ai)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                status = response.getcode()
                raw = response.read()
                encoding = response.headers.get_content_charset() or "utf-8"
                return raw.decode(encoding, errors="replace"), status, None
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                logger.warning(f"Access restricted (HTTP {e.code}) for {url}. Respecting access boundaries without bypass.")
            else:
                logger.warning(f"HTTP {e.code} fetching {url}: {e.reason}")
            return None, e.code, str(e)
        except Exception as e:
            logger.warning(f"Network error fetching {url}: {e}")
            return None, None, str(e)

    def extract_page_data(
        self,
        html_content: str,
        entry: RegisteredSourceEntry
    ) -> Dict[str, Any]:
        """Parses HTML into structured sections, author, publication date, and headings."""
        parser = StructuredTextHTMLParser()
        parser.feed(html_content)
        parser.close()

        resolved_title = parser.title or entry.title
        resolved_author = parser.meta_author or entry.author or ("SAP SE" if "official" in entry.source_category else "SAP Community")
        resolved_date = parser.meta_date or entry.publication_date

        return {
            "source_id": entry.source_id,
            "url": entry.url,
            "canonical_url": entry.canonical_url,
            "source_category": entry.source_category,
            "provenance_tier": entry.provenance_tier,
            "domain_module": entry.domain_module,
            "title": resolved_title,
            "product_name": entry.product_name,
            "product_version": entry.product_version,
            "author": resolved_author,
            "publication_date": resolved_date,
            "authority_weight": entry.authority_weight,
            "sections": parser.sections
        }

    def chunk_extracted_data(
        self,
        extracted: Dict[str, Any],
        max_chunk_chars: int = 1200,
        min_chunk_chars: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Creates structure-aware, hierarchical chunks preserving canonical URLs,
        product metadata, headings, and page citations.
        """
        chunks = []
        canonical_url = extracted["canonical_url"]

        for sec in extracted.get("sections", []):
            heading = sec.get("heading", "Overview")
            sec_text = sec.get("text", "").strip()
            if len(sec_text) < min_chunk_chars:
                continue

            # Split paragraphs or sub-blocks if section is long
            paragraphs = sec_text.split("\n")
            current_buffer = []
            current_len = 0

            for p in paragraphs:
                p_clean = p.strip()
                if not p_clean:
                    continue
                if current_len + len(p_clean) > max_chunk_chars and current_buffer:
                    chunk_body = " ".join(current_buffer).strip()
                    c_hash = hashlib.sha256(chunk_body.encode("utf-8")).hexdigest()
                    
                    # Content deduplication
                    if c_hash not in self._seen_content_hashes:
                        self._seen_content_hashes.add(c_hash)
                        chunks.append({
                            "source_id": extracted["source_id"],
                            "canonical_url": canonical_url,
                            "source_title": extracted["title"],
                            "source_category": extracted["source_category"],
                            "provenance_tier": extracted["provenance_tier"],
                            "domain_module": extracted["domain_module"],
                            "product_name": extracted["product_name"],
                            "product_version": extracted["product_version"],
                            "author": extracted["author"],
                            "publication_date": extracted["publication_date"],
                            "heading": heading,
                            "chunk_text": chunk_body,
                            "content_hash": c_hash,
                            "authority_weight": extracted["authority_weight"]
                        })
                    current_buffer = [p_clean]
                    current_len = len(p_clean)
                else:
                    current_buffer.append(p_clean)
                    current_len += len(p_clean)

            if current_buffer:
                chunk_body = " ".join(current_buffer).strip()
                if len(chunk_body) >= min_chunk_chars:
                    c_hash = hashlib.sha256(chunk_body.encode("utf-8")).hexdigest()
                    if c_hash not in self._seen_content_hashes:
                        self._seen_content_hashes.add(c_hash)
                        chunks.append({
                            "source_id": extracted["source_id"],
                            "canonical_url": canonical_url,
                            "source_title": extracted["title"],
                            "source_category": extracted["source_category"],
                            "provenance_tier": extracted["provenance_tier"],
                            "domain_module": extracted["domain_module"],
                            "product_name": extracted["product_name"],
                            "product_version": extracted["product_version"],
                            "author": extracted["author"],
                            "publication_date": extracted["publication_date"],
                            "heading": heading,
                            "chunk_text": chunk_body,
                            "content_hash": c_hash,
                            "authority_weight": extracted["authority_weight"]
                        })

        return chunks

    def process_registered_source(
        self,
        entry: RegisteredSourceEntry,
        mock_html: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Processes a single source entry according to its category and discovery flags.
        Enforces discovery guardrail: Discovery sources are NEVER ingested wholesale.
        """
        # Rule 1: Discovery portal and organization hubs
        if entry.is_discovery_source:
            logger.info(f"Skipping wholesale ingestion for discovery source: {entry.canonical_url} ({entry.title})")
            return {
                "source_id": entry.source_id,
                "canonical_url": entry.canonical_url,
                "status": "skipped_discovery_source",
                "reason": "Discovery portal/organization root; inspected for links, not ingested wholesale.",
                "chunks_count": 0
            }

        # Rule 2: URL Deduplication
        if entry.canonical_url in self._seen_canonical_urls:
            logger.info(f"Skipping duplicate canonical URL: {entry.canonical_url}")
            return {
                "source_id": entry.source_id,
                "canonical_url": entry.canonical_url,
                "status": "skipped_duplicate_url",
                "reason": "Canonical URL already processed in this ingestion run.",
                "chunks_count": 0
            }
        self._seen_canonical_urls.add(entry.canonical_url)

        # Rule 3: Retrieve Page Content
        if mock_html:
            html_text = mock_html
            err = None
        else:
            html_text, status_code, err = self.fetch_url_content(entry.url)

        if not html_text:
            return {
                "source_id": entry.source_id,
                "canonical_url": entry.canonical_url,
                "status": "fetch_failed_or_restricted",
                "reason": err or "No content returned or access restricted",
                "chunks_count": 0
            }

        # Rule 4: Extract Structured Sections
        extracted = self.extract_page_data(html_text, entry)

        # Rule 5: Chunking & Content Deduplication
        chunks = self.chunk_extracted_data(extracted)

        return {
            "source_id": entry.source_id,
            "canonical_url": entry.canonical_url,
            "status": "ingested_successfully",
            "title": extracted["title"],
            "category": entry.source_category,
            "provenance_tier": entry.provenance_tier,
            "chunks_count": len(chunks),
            "chunks": chunks
        }

    def run_full_registry_ingestion(
        self,
        db: Optional[Session] = None,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        Executes ingestion for all registered sources in source_registry.
        Guarantees isolation: writes to web_doc_snapshots and optionally web_doc_chunks table.
        """
        all_entries = self.registry.get_all()
        results = []
        all_chunks = []
        discovery_sources_count = 0
        deduped_urls_count = 0
        success_count = 0
        failed_count = 0

        for entry in all_entries:
            # Skip GitHub repositories (handled by ingest_github_repositories.py)
            if entry.source_category == "github_repo":
                continue

            if entry.is_discovery_source:
                discovery_sources_count += 1
                res = self.process_registered_source(entry)
                results.append(res)
                continue

            res = self.process_registered_source(entry)
            results.append(res)

            if res["status"] == "ingested_successfully":
                success_count += 1
                chunks = res.get("chunks", [])
                all_chunks.extend(chunks)

                # Persist to database if provided and not dry_run
                if db and not dry_run:
                    for chk in chunks:
                        record = WebDocChunk(
                            source_id=chk["source_id"],
                            canonical_url=chk["canonical_url"],
                            source_title=chk["source_title"],
                            source_category=chk["source_category"],
                            provenance_tier=chk["provenance_tier"],
                            domain_module=chk["domain_module"],
                            product_name=chk.get("product_name"),
                            product_version=chk.get("product_version"),
                            author=chk.get("author"),
                            publication_date=chk.get("publication_date"),
                            heading=chk.get("heading"),
                            chunk_text=chk["chunk_text"],
                            content_hash=chk["content_hash"],
                            authority_weight=chk["authority_weight"]
                        )
                        db.add(record)
                    db.commit()

            elif res["status"] == "skipped_duplicate_url":
                deduped_urls_count += 1
            else:
                failed_count += 1

        # Write snapshot artifact to backend/data/web_doc_snapshots/web_doc_chunks.json
        if not dry_run:
            snapshot_file = self.snapshots_dir / "web_doc_chunks.json"
            snapshot_file.write_text(json.dumps(all_chunks, indent=2), encoding="utf-8")
            logger.info(f"Saved {len(all_chunks)} web doc chunks to {snapshot_file}")

        summary = {
            "total_sources_evaluated": len([e for e in all_entries if e.source_category != "github_repo"]),
            "discovery_sources_identified_and_skipped": discovery_sources_count,
            "successful_sources_ingested": success_count,
            "deduplicated_urls_skipped": deduped_urls_count,
            "failed_or_restricted_sources": failed_count,
            "total_retained_chunks": len(all_chunks),
            "total_unique_content_hashes": len(self._seen_content_hashes),
            "snapshot_path": str(self.snapshots_dir / "web_doc_chunks.json"),
            "details": results
        }

        # Save summary report
        summary_file = self.snapshots_dir / "ingestion_summary.json"
        summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        return summary


sap_web_doc_ingest_service = SAPWebDocIngestService()
