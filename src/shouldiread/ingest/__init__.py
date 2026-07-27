"""Ingest layer: discovery + article fetching."""

from .client import fetch, fetch_many, fetch_raw, load_cached
from .discovery import Discovered, article_id_from_url, from_atom, from_sitemap, monthly_sitemaps, recent

__all__ = [
    "Discovered",
    "article_id_from_url",
    "fetch",
    "fetch_many",
    "fetch_raw",
    "from_atom",
    "from_sitemap",
    "load_cached",
    "monthly_sitemaps",
    "recent",
]
