#!/usr/bin/env python3
"""
extractor.py — content-hash cache for Claude Code's document extractions.

Claude Code reads raw document text during validation, extracts structured data
using its own reasoning, then persists results here via the save_extraction /
load_extraction MCP tools.  Subsequent runs skip re-extraction on unchanged files.

Cache file: intermediate/extraction_cache.json
Cache key:  sha256(file_content) — invalidates automatically when source changes

Public API
----------
  ContentHashCache   — hash-keyed JSON store (used by MCP server)
  hash_file(path)    — SHA-256 of a file's bytes
  hash_text(text)    — SHA-256 of a string
"""

import hashlib
import json
import os
from typing import Optional

_ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_PATH = os.path.join(_ROOT, "intermediate", "extraction_cache.json")


class ContentHashCache:
    """Persist Claude's document extractions keyed by SHA-256 content hash.

    Writes to intermediate/extraction_cache.json on every set() call so
    results survive process restarts.
    """

    def __init__(self, path: str = _CACHE_PATH):
        self.path = path
        self._store: dict = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self._store = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._store = {}

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._store, f, indent=2)

    def get(self, key: str) -> Optional[dict]:
        return self._store.get(key)

    def set(self, key: str, value: dict):
        self._store[key] = value
        self._save()

    def delete(self, key: str):
        self._store.pop(key, None)
        self._save()

    def keys(self) -> list:
        return list(self._store.keys())

    def stats(self) -> dict:
        """Counts of cached entries by doc_type prefix."""
        counts: dict = {}
        for k in self._store:
            prefix = k.split(":")[0]
            counts[prefix] = counts.get(prefix, 0) + 1
        return counts


def hash_file(path: str) -> str:
    """Return hex SHA-256 of a file's raw bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_text(text: str) -> str:
    """Return hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(text.encode()).hexdigest()


# Module-level cache instance shared by the MCP server
_cache = ContentHashCache()
