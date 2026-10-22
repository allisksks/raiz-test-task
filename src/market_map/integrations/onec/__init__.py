"""Интеграция с 1С через стандартный интерфейс OData."""

from market_map.integrations.onec.loader import load_onec
from market_map.integrations.onec.source import FileSource, HttpSource, OneCSource
from market_map.loads import LoadResult

__all__ = ["FileSource", "HttpSource", "LoadResult", "OneCSource", "load_onec"]
