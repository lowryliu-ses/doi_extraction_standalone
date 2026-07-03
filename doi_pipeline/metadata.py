from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

BAD_TITLE_RE = re.compile(
    r"^(?:abstract|keywords|print|untitled|microsoft word|pdfthreading|crossmark_default|"
    r"crossmark|template for electronic submission|doi\s*:|url\s*:)$",
    re.IGNORECASE,
)


def clean(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def norm_text(value: Any) -> str:
    text = clean(value).lower()
    text = (
        text.replace("β", "beta")
        .replace("α", "alpha")
        .replace("γ", "gamma")
        .replace("–", "-")
        .replace("—", "-")
        .replace("‐", "-")
        .replace("‑", "-")
        .replace("−", "-")
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\W_]+", " ", text, flags=re.U)
    return re.sub(r"\s+", " ", text).strip()


def is_bad_title(value: Any) -> bool:
    text = clean(value)
    normalized = norm_text(text)
    return not normalized or len(normalized) < 5 or bool(BAD_TITLE_RE.match(text)) or normalized in {"print", "pdfthreading", "crossmark default"}


def parse_authors(value: Any) -> list[str]:
    if isinstance(value, list):
        authors: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = clean(item.get("name") or item.get("author") or item.get("full_name"))
            else:
                text = clean(item)
            if text:
                authors.append(text)
        return authors
    text = clean(value)
    if not text:
        return []
    return [part.strip() for part in re.split(r";|, and | and ", text) if part.strip()]


def load_grobid_metadata(path: Path | None) -> dict[str, dict[str, str]]:
    if not path:
        return {}
    expanded = path.expanduser()
    rows: list[dict[str, str]] = []
    if expanded.suffix.lower() == ".jsonl":
        with expanded.open(encoding="utf-8", errors="replace") as file:
            for line in file:
                if line.strip():
                    value = json.loads(line)
                    if isinstance(value, dict):
                        rows.append({str(key): clean(item) for key, item in value.items()})
    else:
        with expanded.open(encoding="utf-8", errors="replace", newline="") as file:
            rows.extend({key: clean(value) for key, value in row.items()} for row in csv.DictReader(file, delimiter="\t"))
    by_name: dict[str, dict[str, str]] = {}
    for row in rows:
        source_pdf = row.get("source_pdf") or row.get("filename") or row.get("path") or ""
        if not source_pdf:
            source_pdf = (row.get("tei_xml") or "").removesuffix(".tei.xml").removesuffix(".header.tei.xml")
        name = Path(source_pdf).name.lower()
        if name:
            by_name[name] = row
    return by_name


def merge_grobid(pdf_metadata: dict[str, Any], grobid: dict[str, str] | None) -> tuple[dict[str, Any], str, str, list[str]]:
    merged = dict(pdf_metadata)
    title_source = "pdf" if merged.get("title") else ""
    authors_source = "pdf" if merged.get("authors") else ""
    contributors: list[str] = []
    if not grobid:
        return merged, title_source, authors_source, contributors
    if grobid.get("title") and is_bad_title(merged.get("title")):
        merged["title"] = grobid["title"]
        title_source = "grobid"
        contributors.append("grobid")
    grobid_authors = parse_authors(grobid.get("authors"))
    if grobid_authors and not merged.get("authors"):
        merged["authors"] = grobid_authors
        authors_source = "grobid"
        contributors.append("grobid")
    for field in ("year", "journal", "publisher", "abstract"):
        if not merged.get(field) and grobid.get(field):
            merged[field] = int(grobid[field]) if field == "year" and grobid[field].isdigit() else grobid[field]
            contributors.append("grobid")
    return merged, title_source, authors_source, list(dict.fromkeys(contributors))


def author_score(query_authors: list[str], work_authors: list[str]) -> float:
    if not query_authors or not work_authors:
        return 0.0
    def keys(author: str) -> set[str]:
        tokens = [token for token in norm_text(author).split() if len(token) >= 2]
        if not tokens:
            return set()
        return {"".join(tokens), tokens[-1]}
    work_keys = set()
    for author in work_authors:
        work_keys.update(keys(author))
    if not work_keys:
        return 0.0
    matched = 0
    for author in query_authors:
        if any(key in work_keys or any(key in wk or wk in key for wk in work_keys) for key in keys(author)):
            matched += 1
    return matched / max(len(query_authors), 1)


def title_score(left: Any, right: Any) -> float:
    left_norm = norm_text(left)
    right_norm = norm_text(right)
    if not left_norm or not right_norm or is_bad_title(left) or is_bad_title(right):
        return 0.0
    if left_norm == right_norm:
        return 1.0
    import difflib

    ratio = difflib.SequenceMatcher(None, left_norm, right_norm).ratio()
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    overlap = len(left_tokens & right_tokens) / max(len(left_tokens), 1)
    containment = 0.0
    if left_norm in right_norm or right_norm in left_norm:
        containment = min(len(left_norm), len(right_norm)) / max(len(left_norm), len(right_norm))
    return max(ratio, overlap * 0.94, containment)
