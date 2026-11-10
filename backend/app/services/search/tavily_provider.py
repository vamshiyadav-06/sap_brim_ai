"""Tavily search provider adapter."""

import logging
import re
from typing import List, Optional, Dict, Any

from backend.app.config import settings
from backend.app.services.search.base import (
    SearchHit,
    ImageHit,
    SearchProviderError,
    SearchProviderUnavailableError,
)

logger = logging.getLogger(__name__)


class TavilySearchProvider:
    """Adapter wrapping TavilyClient into SearchProviderInterface."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key if api_key is not None else settings.WEB_SEARCH_API_KEY
        self._client = None
        # An explicit sentinel makes offline test/staging environments fail closed even when .env
        # values would otherwise be inherited. Empty env values can be ignored by BaseSettings.
        if self.api_key and self.api_key.strip().lower() not in {"disabled", "none", "off"}:
            try:
                from tavily import TavilyClient
                self._client = TavilyClient(api_key=self.api_key)
            except Exception as exc:
                logger.warning("[Tavily] Failed to initialize TavilyClient: %s", exc)
                self._client = None

    @property
    def is_available(self) -> bool:
        return self._client is not None

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        search_depth: str = "basic",
        topic: str = "general",
        time_range: Optional[str] = None,
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
        include_raw_content: bool = True,
        include_images: bool = False,
    ) -> List[SearchHit]:
        if not self._client:
            raise SearchProviderUnavailableError("Tavily API key is not configured or client unavailable")

        try:
            kwargs: Dict[str, Any] = {
                "query": query,
                "search_depth": search_depth,
                "max_results": max_results,
                "include_raw_content": include_raw_content,
                "include_images": include_images,
                "include_answer": False,
            }
            if include_domains:
                kwargs["include_domains"] = include_domains
            if exclude_domains:
                kwargs["exclude_domains"] = exclude_domains

            # An explicit, short timeout: the SDK default is 60 s, which would hold a user request that long.
            kwargs["timeout"] = int(settings.WEB_SEARCH_TIMEOUT_SECONDS)
            response = self._client.search(**kwargs)
            results = response.get("results", []) if isinstance(response, dict) else []

            hits: List[SearchHit] = []
            for item in results:
                url = item.get("url", "").strip()
                if not url:
                    continue
                hits.append(SearchHit(
                    title=item.get("title") or "Tavily Result",
                    url=url,
                    snippet=item.get("content") or "",
                    raw_content=item.get("raw_content"),
                    score=float(item.get("score") or 1.0),
                    published_date=item.get("published_date"),
                    engine="tavily",
                ))
            return hits
        except Exception as exc:
            raise SearchProviderError(f"Tavily search failed: {exc}")

    def search_images(
        self,
        query: str,
        *,
        max_images: int = 4,
        include_domains: Optional[List[str]] = None,
    ) -> List[ImageHit]:
        if not self._client:
            return []

        try:
            search_kwargs = dict(
                query=query,
                search_depth="basic",
                max_results=max_images * 2,
                include_images=True,
                include_image_descriptions=True,
                include_raw_content=False,
                include_answer=False,
            )
            if include_domains:
                search_kwargs["include_domains"] = include_domains
            search_kwargs["timeout"] = int(settings.WEB_SEARCH_TIMEOUT_SECONDS)
            response = self._client.search(**search_kwargs)
            raw_images = response.get("images") or response.get("image_results") or []
            hits: List[ImageHit] = []
            for img in raw_images:
                url = img if isinstance(img, str) else img.get("url")
                if not url:
                    continue
                hits.append(ImageHit(
                    url=url,
                    source_url=img.get("source_url") if isinstance(img, dict) else None,
                    source_title=img.get("source_title") if isinstance(img, dict) else None,
                    description=img.get("description") if isinstance(img, dict) else None,
                    alt_text="Technical visual",
                ))
                if len(hits) >= max_images:
                    break
            return hits
        except Exception as exc:
            logger.warning("[Tavily] Image search failed: %s", exc)
            return []
