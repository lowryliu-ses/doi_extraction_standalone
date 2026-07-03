from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .doi import normalize_doi
from .metadata import author_score, clean, parse_authors, title_score


def is_error_value(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get("error"))


def load_cache(path: Path) -> dict[str, Any]:
    cache: dict[str, Any] = {}
    if not path.exists():
        return cache
    with path.open(encoding="utf-8", errors="replace") as file:
        for line in file:
            if not line.strip():
                continue
            item = json.loads(line)
            value = item.get("value")
            # Error values from older runs are transient failures; drop them so they retry.
            if not is_error_value(value):
                cache[str(item.get("key"))] = value
    return cache


def append_cache(path: Path, key: str, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"key": key, "value": value, "time": time.time()}, ensure_ascii=False) + "\n")


def request_json(url: str, timeout: float, user_agent: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class MetadataValidator:
    def __init__(
        self,
        cache_path: Path,
        *,
        timeout: float = 20.0,
        min_interval: float = 0.2,
        user_agent: str = "doi-standalone/0.1",
        mailto: str = "",
    ) -> None:
        self.cache_path = cache_path
        self.cache = load_cache(cache_path)
        self.timeout = timeout
        self.min_interval = min_interval
        # A mailto in the User-Agent opts into Crossref's polite pool (better rate limits).
        self.user_agent = f"{user_agent} (mailto:{mailto})" if mailto else user_agent
        self.cache_lock = threading.Lock()
        self.rate_lock = threading.Lock()
        self.next_request = 0.0

    def cached_json(self, key: str, url: str) -> Any:
        with self.cache_lock:
            if key in self.cache:
                return self.cache[key]
        with self.rate_lock:
            wait = self.next_request - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.next_request = time.monotonic() + self.min_interval
        try:
            value = request_json(url, self.timeout, self.user_agent)
        except Exception as exc:
            value = {"error": f"{type(exc).__name__}: {exc}"}
        with self.cache_lock:
            if key in self.cache:
                return self.cache[key]
            self.cache[key] = value
            # Errors stay in memory to avoid hammering within this run,
            # but are not persisted so the next run retries them.
            if not is_error_value(value):
                append_cache(self.cache_path, key, value)
        return value

    def crossref_work(self, doi: str) -> dict[str, Any] | None:
        doi = normalize_doi(doi)
        if not doi:
            return None
        url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
        data = self.cached_json(f"crossref:doi:{doi.lower()}", url)
        if isinstance(data, dict) and data.get("status") == "ok" and isinstance(data.get("message"), dict):
            return data["message"]
        return None

    def title_search(self, title: str) -> list[str]:
        title = clean(title)
        if len(title) < 18:
            return []
        query = urllib.parse.urlencode({"query.title": title, "rows": "5"})
        data = self.cached_json(f"crossref:title:{title}", f"https://api.crossref.org/works?{query}")
        message = data.get("message") if isinstance(data, dict) else None
        items = message.get("items") if isinstance(message, dict) else None
        return [normalize_doi(item.get("DOI")) for item in items if isinstance(item, dict) and item.get("DOI")] if isinstance(items, list) else []

    def verify(self, doi: str, title: str, authors: list[str]) -> dict[str, Any]:
        work = self.crossref_work(doi)
        if not work:
            return {"verified": False, "doi": normalize_doi(doi), "reason": "crossref_not_found"}
        work_title = clean((work.get("title") or [""])[0] if isinstance(work.get("title"), list) else work.get("title"))
        work_authors = []
        for item in work.get("author", []) if isinstance(work.get("author"), list) else []:
            if isinstance(item, dict):
                name = " ".join(part for part in (clean(item.get("given")), clean(item.get("family"))) if part)
                if name:
                    work_authors.append(name)
        t_score = title_score(title, work_title)
        a_score = author_score(authors, work_authors)
        verified = t_score >= 0.94 and (a_score >= 0.15 or (t_score >= 0.985 and not authors))
        return {
            "verified": verified,
            "doi": normalize_doi(doi),
            "title_score": round(t_score, 4),
            "author_score": round(a_score, 4),
            "work_title": work_title,
            "work_authors": "; ".join(work_authors[:8]),
            "reason": "verified" if verified else "score_below_threshold",
        }
