#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from doi_pipeline.doi import doi_from_elsevier_pii, is_plausible_doi, normalize_doi
from doi_pipeline.metadata import author_score, clean, is_bad_title, norm_text, parse_authors, title_score

csv.field_size_limit(sys.maxsize)

BAD_LOCAL_TITLE_RE = re.compile(
    r"(accepted manuscript|to cite this version|self-archived|metadata of the chapter|"
    r"dissertation|master'?s thesis|copyright|downloaded by|view article online|"
    r"abstract of the dissertation|peer review information|editor summary|showcasing research|"
    r"prepared as an account|we accept this|required standard|eingereicht|notice: subject|"
    r"prior publication|issue information|table of contents|contents|downloaded from|"
    r"terms of use|all rights reserved|this article is protected|supplementary material)",
    re.IGNORECASE,
)

STOPWORDS = {
    "and",
    "for",
    "the",
    "with",
    "from",
    "this",
    "that",
    "into",
    "using",
    "based",
    "study",
    "effect",
    "properties",
    "materials",
}

FILENAME_SOURCES = {"filename", "filename_doi", "filename_pii", "filename_ssrn", "filename_variant", "filename_main_doi"}


@dataclass(frozen=True)
class Candidate:
    doi: str
    source: str
    reason: str


@dataclass
class Authority:
    resolved: bool
    doi: str
    source: str = ""
    authority_doi: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    container: str = ""
    publisher: str = ""
    year: str = ""
    type: str = ""
    error: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "authority_resolved": "1" if self.resolved else "0",
            "authority_source": self.source,
            "authority_doi_new": self.authority_doi,
            "authority_title_new": self.title,
            "authority_authors_new": "; ".join(self.authors),
            "authority_container_new": self.container,
            "authority_publisher_new": self.publisher,
            "authority_year_new": self.year,
            "authority_type_new": self.type,
            "authority_error_new": self.error,
        }


def compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def title_is_bad(value: Any) -> bool:
    text = clean(value)
    normalized = norm_text(text)
    return is_bad_title(text) or len(normalized) < 8 or bool(BAD_LOCAL_TITLE_RE.search(text))


def csl_text(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            text = clean(item)
            if text:
                return text
        return ""
    return clean(value)


def csl_author_name(author: dict[str, Any]) -> str:
    literal = clean(author.get("literal"))
    if literal:
        return literal
    return " ".join(part for part in (clean(author.get("given")), clean(author.get("family"))) if part)


def issued_year(value: Any) -> str:
    if isinstance(value, dict):
        parts = value.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            return clean(parts[0][0])
    return ""


def normalize_candidate(value: Any) -> str:
    doi = normalize_doi(value)
    return doi if doi and is_plausible_doi(doi) else ""


def add_candidate(candidates: list[Candidate], seen: set[str], doi: str, source: str, reason: str) -> None:
    normalized = normalize_candidate(doi)
    if not normalized:
        return
    key = normalized.lower()
    if key in seen:
        return
    seen.add(key)
    candidates.append(Candidate(normalized, source, reason))


def filename_stem(row: dict[str, Any]) -> str:
    return Path(clean(row.get("filename"))).stem


def doi_like_tokens(text: str) -> list[str]:
    out: list[str] = []
    for match in re.finditer(r"10\.\d{4,9}[_@-][A-Za-z0-9][A-Za-z0-9._;()/:+@-]*", text, re.IGNORECASE):
        out.append(match.group(0).rstrip("._-"))
    return out


def split_filename_token(token: str) -> tuple[str, str] | None:
    token = re.sub(r"(?i)\.pdf$", "", token.strip())
    token = token.replace("@", "_")
    match = re.match(r"^(10\.\d{4,9})[_-](.+)$", token, re.IGNORECASE)
    if not match:
        return None
    prefix, suffix = match.groups()
    suffix = suffix.strip("._-")
    return prefix, suffix


def doi_variants_from_token(token: str) -> list[Candidate]:
    split = split_filename_token(token)
    if not split:
        pii = doi_from_elsevier_pii(token)
        return [Candidate(pii, "filename_pii", "Elsevier PII in filename")] if pii else []
    prefix, suffix = split
    variants: list[Candidate] = []
    seen: set[str] = set()

    def add(suffix_value: str, reason: str, source: str = "filename_variant") -> None:
        add_candidate(variants, seen, f"{prefix}/{suffix_value}", source, reason)

    add(suffix, "filename prefix separator")
    lower_prefix = prefix.lower()
    lower_suffix = suffix.lower()
    if "_" in suffix:
        if lower_prefix in {"10.5445", "10.26153", "10.4233", "10.6092", "10.17635"}:
            add(suffix.upper().replace("_", "/"), "repository uppercase path DOI")
        add(suffix.replace("_", "/", 1), "first underscore changed to slash")
        add(suffix.replace("_", "/"), "all underscores changed to slashes")
    if lower_prefix == "10.1149":
        if re.match(r"^\d{4}-\d{4}_[A-Za-z0-9]", suffix):
            add(suffix.replace("_", "/", 1), "ECS ISSN/article DOI")
        if re.match(r"^ma\d{4}-\d{2}_\d+_\d+$", lower_suffix):
            parts = suffix.replace("_", "/")
            parts = re.sub(r"^ma", "MA", parts, flags=re.IGNORECASE)
            add(parts, "ECS meeting abstract DOI")
    if lower_prefix == "10.1021" and re.search(r"\.s\d{3,4}$", suffix, re.IGNORECASE):
        add(re.sub(r"\.s\d{3,4}$", "", suffix, flags=re.IGNORECASE), "ACS supplementary DOI stripped", "filename_main_doi")
    if lower_prefix == "10.1039":
        add(suffix.replace("_", ""), "RSC compact DOI")
    if lower_prefix in {"10.5445", "10.26153", "10.4233", "10.6092", "10.17635"} and "_" in suffix:
        add(suffix.replace("_", "/"), "repository underscore path DOI")
        add(suffix.upper().replace("_", "/"), "repository uppercase path DOI")
    return variants


def build_candidates(row: dict[str, Any]) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for field, source in (("doi", "existing_doi"), ("final_doi", "final_doi"), ("authority_doi", "previous_authority_doi")):
        add_candidate(candidates, seen, row.get(field, ""), source, field)
    for token in doi_like_tokens(filename_stem(row)):
        for candidate in doi_variants_from_token(token):
            add_candidate(candidates, seen, candidate.doi, candidate.source, candidate.reason)
    return candidates


class Cache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {}
        if path.exists():
            with path.open(encoding="utf-8", errors="replace") as file:
                for line in file:
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    key = clean(item.get("key"))
                    if key:
                        self.data[key] = item.get("value")

    def get(self, key: str) -> Any:
        with self.lock:
            return self.data.get(key)

    def set(self, key: str, value: Any) -> None:
        with self.lock:
            self.data[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps({"key": key, "value": value, "time": time.time()}, ensure_ascii=False, separators=(",", ":")) + "\n")


def request_json(url: str, timeout: float, accept: str = "application/json") -> Any:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "doi-hardcase-resolver/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def as_error(doi: str, source: str, exc: Exception) -> Authority:
    return Authority(False, doi, source=source, error=f"{type(exc).__name__}: {exc}")


def parse_doi_org(doi: str, data: dict[str, Any]) -> Authority:
    authors = [csl_author_name(author) for author in data.get("author", []) if isinstance(author, dict)]
    return Authority(
        True,
        doi,
        source="doi.org",
        authority_doi=normalize_candidate(data.get("DOI") or doi) or doi,
        title=csl_text(data.get("title")),
        authors=[author for author in authors if author],
        container=csl_text(data.get("container-title")),
        publisher=csl_text(data.get("publisher")),
        year=issued_year(data.get("issued")),
        type=csl_text(data.get("type")),
    )


def parse_crossref(doi: str, data: dict[str, Any]) -> Authority:
    message = data.get("message") if isinstance(data, dict) else {}
    authors = []
    for item in message.get("author", []) if isinstance(message, dict) else []:
        if isinstance(item, dict):
            name = " ".join(part for part in (clean(item.get("given")), clean(item.get("family"))) if part)
            if name:
                authors.append(name)
    return Authority(
        True,
        doi,
        source="crossref",
        authority_doi=normalize_candidate(message.get("DOI") or doi) or doi,
        title=csl_text(message.get("title")),
        authors=authors,
        container=csl_text(message.get("container-title")),
        publisher=csl_text(message.get("publisher")),
        year=issued_year(message.get("issued")),
        type=csl_text(message.get("type")),
    )


def parse_datacite(doi: str, data: dict[str, Any]) -> Authority:
    attributes = ((data.get("data") or {}).get("attributes") or {}) if isinstance(data, dict) else {}
    titles = attributes.get("titles") or []
    creators = attributes.get("creators") or []
    title = ""
    for item in titles:
        if isinstance(item, dict) and clean(item.get("title")):
            title = clean(item.get("title"))
            break
    authors = [clean(item.get("name")) for item in creators if isinstance(item, dict) and clean(item.get("name"))]
    return Authority(
        True,
        doi,
        source="datacite",
        authority_doi=normalize_candidate(attributes.get("doi") or doi) or doi,
        title=title,
        authors=authors,
        container="",
        publisher=clean(attributes.get("publisher")),
        year=clean(attributes.get("publicationYear")),
        type=clean(((attributes.get("types") or {}).get("resourceTypeGeneral") if isinstance(attributes.get("types"), dict) else "")),
    )


def parse_osti(doi: str, data: Any) -> Authority:
    item = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else {}
    authors_raw = item.get("authors") or item.get("author") or []
    authors: list[str] = []
    if isinstance(authors_raw, list):
        for author in authors_raw:
            if isinstance(author, dict):
                name = clean(author.get("full_name") or author.get("name"))
            else:
                name = clean(author)
            if name:
                authors.append(name)
    elif clean(authors_raw):
        authors = parse_authors(authors_raw)
    return Authority(
        True,
        doi,
        source="osti",
        authority_doi=normalize_candidate(item.get("doi") or doi) or doi,
        title=clean(item.get("title")),
        authors=authors,
        container=clean(item.get("journal_name")),
        publisher="OSTI",
        year=clean(item.get("publication_date") or item.get("publication_year"))[:4],
        type=clean(item.get("resource_type")),
    )


class Resolver:
    def __init__(self, cache_path: Path, timeout: float) -> None:
        self.cache = Cache(cache_path)
        self.timeout = timeout

    def cached(self, key: str, fn: Any) -> Any:
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        try:
            value = fn()
        except Exception as exc:
            value = {"error": f"{type(exc).__name__}: {exc}"}
        self.cache.set(key, value)
        time.sleep(0.03)
        return value

    def doi_org(self, doi: str) -> Authority:
        url = "https://doi.org/" + urllib.parse.quote(doi, safe="/")
        data = self.cached(f"doi.org:{doi.lower()}", lambda: request_json(url, self.timeout, "application/vnd.citationstyles.csl+json"))
        if isinstance(data, dict) and not data.get("error"):
            return parse_doi_org(doi, data)
        return Authority(False, doi, source="doi.org", error=clean((data or {}).get("error") if isinstance(data, dict) else data))

    def crossref(self, doi: str) -> Authority:
        url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
        data = self.cached(f"crossref:{doi.lower()}", lambda: request_json(url, self.timeout))
        if isinstance(data, dict) and data.get("status") == "ok" and isinstance(data.get("message"), dict):
            return parse_crossref(doi, data)
        return Authority(False, doi, source="crossref", error=clean((data or {}).get("error") if isinstance(data, dict) else data))

    def datacite(self, doi: str) -> Authority:
        url = "https://api.datacite.org/dois/" + urllib.parse.quote(doi, safe="")
        data = self.cached(f"datacite:{doi.lower()}", lambda: request_json(url, self.timeout))
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return parse_datacite(doi, data)
        return Authority(False, doi, source="datacite", error=clean((data or {}).get("error") if isinstance(data, dict) else data))

    def osti(self, doi: str) -> Authority:
        suffix = doi.split("/", 1)[1] if "/" in doi else doi
        urls = [
            "https://www.osti.gov/api/v1/records?doi=" + urllib.parse.quote(doi, safe="/"),
            "https://www.osti.gov/api/v1/records/" + urllib.parse.quote(suffix, safe=""),
        ]
        errors = []
        for url in urls:
            data = self.cached(f"osti:{url}", lambda url=url: request_json(url, self.timeout))
            if isinstance(data, list) and data:
                return parse_osti(doi, data)
            if isinstance(data, dict) and data and not data.get("error"):
                return parse_osti(doi, data)
            errors.append(clean((data or {}).get("error") if isinstance(data, dict) else data))
        return Authority(False, doi, source="osti", error=" | ".join(errors))

    def resolve(self, doi: str) -> Authority:
        doi = normalize_candidate(doi)
        if not doi:
            return Authority(False, "", error="invalid DOI")
        sources = []
        if doi.lower().startswith("10.2172/"):
            sources.append(self.osti)
        sources.extend([self.doi_org, self.crossref, self.datacite])
        errors: list[str] = []
        for source in sources:
            authority = source(doi)
            if authority.resolved and authority.title:
                return authority
            errors.append(f"{authority.source}: {authority.error}")
        return Authority(False, doi, error="; ".join(errors))

    def crossref_title_search(self, title: str, rows: int = 5) -> list[Authority]:
        query = urllib.parse.urlencode({"query.title": title, "rows": str(rows)})
        url = f"https://api.crossref.org/works?{query}"
        data = self.cached(f"crossref-title:{norm_text(title)}", lambda: request_json(url, self.timeout))
        items = (((data or {}).get("message") or {}).get("items") or []) if isinstance(data, dict) else []
        out: list[Authority] = []
        for item in items:
            if isinstance(item, dict) and item.get("DOI"):
                out.append(parse_crossref(normalize_candidate(item.get("DOI")) or clean(item.get("DOI")), {"message": item}))
        return out

    def datacite_title_search(self, title: str, rows: int = 5) -> list[Authority]:
        query = urllib.parse.urlencode({"query": title, "page[size]": str(rows)})
        url = f"https://api.datacite.org/dois?{query}"
        data = self.cached(f"datacite-title:{norm_text(title)}", lambda: request_json(url, self.timeout))
        items = ((data or {}).get("data") or []) if isinstance(data, dict) else []
        out: list[Authority] = []
        for item in items:
            if isinstance(item, dict):
                out.append(parse_datacite(clean(((item.get("attributes") or {}).get("doi") or "")), {"data": item}))
        return out


def pdf_text(path: Path, pages: int) -> str:
    try:
        import fitz  # type: ignore
    except Exception:
        return ""
    try:
        chunks: list[str] = []
        with fitz.open(str(path)) as doc:
            for index in range(min(pages, len(doc))):
                chunks.append(doc[index].get_text("text") or "")
        return "\n".join(chunks)
    except Exception:
        return ""


def authority_title_coverage(authority_title: str, text: str) -> float:
    title_norm = norm_text(authority_title)
    text_norm = norm_text(text)
    if not title_norm or not text_norm:
        return 0.0
    if title_norm in text_norm:
        return 1.0
    tokens = [token for token in title_norm.split() if len(token) >= 4 and token not in STOPWORDS]
    if not tokens:
        return 0.0
    matched = sum(1 for token in tokens if token in text_norm)
    return matched / len(tokens)


def best_title_from_pdf_text(text: str) -> str:
    bad_line = re.compile(r"(copyright|downloaded|journal|volume|issue|abstract|keywords|contents|references|doi:|www\\.)", re.IGNORECASE)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    candidates: list[str] = []
    for line in lines[:80]:
        normalized = norm_text(line)
        if len(normalized) < 8 or len(line) > 240 or bad_line.search(line):
            continue
        if sum(ch.isalpha() for ch in line) < 8:
            continue
        candidates.append(line)
    if not candidates:
        return ""
    candidates.sort(key=lambda value: (len(norm_text(value).split()) >= 5, len(value)), reverse=True)
    return candidates[0]


def decision_for(row: dict[str, Any], candidate: Candidate, authority: Authority, text: str) -> dict[str, Any]:
    local_title = clean(row.get("title"))
    local_authors = parse_authors(row.get("authors"))
    ts = title_score(local_title, authority.title)
    aus = author_score(local_authors, authority.authors)
    coverage = authority_title_coverage(authority.title, text)
    bad_title = title_is_bad(local_title)
    from_filename = candidate.source in FILENAME_SOURCES
    from_title_search = candidate.source in {"crossref_title_search", "datacite_title_search"}

    accepted = False
    confidence = "none"
    reason = "no automatic rule matched"

    if not authority.resolved:
        reason = authority.error or "authority lookup failed"
    elif ts >= 0.985:
        accepted, confidence, reason = True, "high", f"local title near-exact authority match ({ts:.3f})"
    elif ts >= 0.94 and (aus >= 0.15 or not local_authors):
        accepted, confidence, reason = True, "high", f"local title/author pass strict authority match ({ts:.3f}/{aus:.3f})"
    elif coverage >= 0.78 and len(norm_text(authority.title).split()) >= 4:
        accepted, confidence, reason = True, "high", f"authority title is present in PDF text ({coverage:.3f})"
    elif from_filename and bad_title and authority.resolved:
        accepted, confidence, reason = True, "medium", "filename DOI resolves and local title is missing/header noise"
    elif from_filename and coverage >= 0.55 and authority.resolved:
        accepted, confidence, reason = True, "medium", f"filename DOI resolves and PDF text has partial title support ({coverage:.3f})"
    elif from_filename and row.get("final_decision") == "no_doi_unresolved" and bad_title and authority.resolved:
        accepted, confidence, reason = True, "medium", "no-DOI row recovered from filename DOI and local metadata was unusable"
    elif from_title_search and ts >= 0.88 and aus >= 0.50:
        accepted, confidence, reason = True, "medium", f"title search candidate has strong author support ({ts:.3f}/{aus:.3f})"
    elif from_title_search and coverage >= 0.78:
        accepted, confidence, reason = True, "medium", f"title search candidate title appears in PDF text ({coverage:.3f})"

    return {
        "hardcase_accept": "1" if accepted else "0",
        "hardcase_confidence": confidence,
        "hardcase_reason": reason,
        "hardcase_candidate_doi": candidate.doi,
        "hardcase_candidate_source": candidate.source,
        "hardcase_candidate_reason": candidate.reason,
        "hardcase_final_doi": (authority.authority_doi or candidate.doi) if accepted else "",
        "hardcase_title_score": f"{ts:.4f}",
        "hardcase_author_score": f"{aus:.4f}",
        "hardcase_pdf_title_coverage": f"{coverage:.4f}",
        "hardcase_local_title_bad": "1" if bad_title else "0",
        **authority.to_row(),
    }


def input_authority(row: dict[str, Any]) -> Authority | None:
    title = clean(row.get("authority_title"))
    doi = normalize_candidate(row.get("authority_doi") or row.get("doi"))
    if not title or not doi:
        return None
    return Authority(
        True,
        doi,
        source="previous_authority",
        authority_doi=doi,
        title=title,
        authors=parse_authors(row.get("authority_authors")),
        container=clean(row.get("authority_container")),
        publisher=clean(row.get("authority_publisher")),
        year=clean(row.get("authority_year")),
        type=clean(row.get("authority_type")),
    )


def process_row(row: dict[str, Any], resolver: Resolver, pages: int) -> dict[str, Any]:
    target_path = Path(clean(row.get("target_path")) or clean(row.get("path")))
    text = pdf_text(target_path, pages) if target_path.exists() else ""
    candidates = build_candidates(row)
    if not candidates and title_is_bad(row.get("title")) and text:
        guessed_title = best_title_from_pdf_text(text)
        if guessed_title:
            row = {**row, "hardcase_pdf_guessed_title": guessed_title}
    else:
        row = {**row, "hardcase_pdf_guessed_title": ""}

    title_for_search = clean(row.get("title"))
    if title_is_bad(title_for_search) and clean(row.get("hardcase_pdf_guessed_title")):
        title_for_search = clean(row.get("hardcase_pdf_guessed_title"))

    tried: list[str] = []
    candidate_authorities: list[tuple[Candidate, Authority]] = []
    previous = input_authority(row)
    if previous:
        doi = normalize_candidate(row.get("doi") or previous.authority_doi)
        candidate_authorities.append((Candidate(doi or previous.authority_doi, clean(row.get("doi_source")) or "previous_candidate", "previous resolved authority"), previous))

    for candidate in candidates:
        tried.append(candidate.doi)
        authority = resolver.resolve(candidate.doi)
        candidate_authorities.append((candidate, authority))

    if not candidate_authorities and title_for_search and not title_is_bad(title_for_search) and len(norm_text(title_for_search)) >= 18:
        for authority in resolver.crossref_title_search(title_for_search, rows=5):
            candidate_authorities.append((Candidate(authority.authority_doi or authority.doi, "crossref_title_search", "title search"), authority))
        for authority in resolver.datacite_title_search(title_for_search, rows=5):
            candidate_authorities.append((Candidate(authority.authority_doi or authority.doi, "datacite_title_search", "title search"), authority))

    best: dict[str, Any] | None = None
    for candidate, authority in candidate_authorities:
        result = decision_for(row, candidate, authority, text)
        if best is None:
            best = result
        best_rank = (best["hardcase_accept"] == "1", {"high": 3, "medium": 2, "low": 1, "none": 0}.get(best["hardcase_confidence"], 0), float(best["hardcase_title_score"]), float(best["hardcase_pdf_title_coverage"]))
        rank = (result["hardcase_accept"] == "1", {"high": 3, "medium": 2, "low": 1, "none": 0}.get(result["hardcase_confidence"], 0), float(result["hardcase_title_score"]), float(result["hardcase_pdf_title_coverage"]))
        if rank > best_rank:
            best = result
    if best is None:
        best = {
            "hardcase_accept": "0",
            "hardcase_confidence": "none",
            "hardcase_reason": "no DOI candidate and no usable title search",
            "hardcase_candidate_doi": "",
            "hardcase_candidate_source": "",
            "hardcase_candidate_reason": "",
            "hardcase_final_doi": "",
            "hardcase_title_score": "0.0000",
            "hardcase_author_score": "0.0000",
            "hardcase_pdf_title_coverage": "0.0000",
            "hardcase_local_title_bad": "1" if title_is_bad(row.get("title")) else "0",
            **Authority(False, "", error="no candidate").to_row(),
        }
    return {
        **row,
        **best,
        "hardcase_tried_dois": "; ".join(dict.fromkeys(tried)),
        "hardcase_pdf_text_chars": str(len(text)),
    }


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve hard DOI metadata cases")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--pdf-pages", type=int, default=4)
    args = parser.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(args.manifest.open(encoding="utf-8", errors="replace"), delimiter="\t"))
    resolver = Resolver(args.output_dir / "hardcase_authority_cache.jsonl", timeout=args.timeout)

    output_rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(process_row, row, resolver, args.pdf_pages): row for row in rows}
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            output_rows.append(result)
            print(
                f"{index}/{len(rows)}\t{result.get('hardcase_accept')}\t{result.get('hardcase_final_doi') or result.get('hardcase_candidate_doi')}\t{result.get('filename')}",
                flush=True,
            )

    output_rows.sort(key=lambda row: (row.get("folder", ""), row.get("filename", "")))
    base_fields = list(rows[0].keys()) if rows else []
    new_fields = [
        "hardcase_accept",
        "hardcase_confidence",
        "hardcase_reason",
        "hardcase_final_doi",
        "hardcase_candidate_doi",
        "hardcase_candidate_source",
        "hardcase_candidate_reason",
        "hardcase_title_score",
        "hardcase_author_score",
        "hardcase_pdf_title_coverage",
        "hardcase_local_title_bad",
        "hardcase_pdf_guessed_title",
        "hardcase_tried_dois",
        "hardcase_pdf_text_chars",
        "authority_resolved",
        "authority_source",
        "authority_doi_new",
        "authority_title_new",
        "authority_authors_new",
        "authority_container_new",
        "authority_publisher_new",
        "authority_year_new",
        "authority_type_new",
        "authority_error_new",
    ]
    fields = new_fields + [field for field in base_fields if field not in new_fields]
    all_tsv = args.output_dir / "hardcase_427_resolution.tsv"
    accepted_tsv = args.output_dir / "hardcase_427_accepted.tsv"
    still_tsv = args.output_dir / "hardcase_427_still_unresolved.tsv"
    accepted = [row for row in output_rows if row.get("hardcase_accept") == "1"]
    still = [row for row in output_rows if row.get("hardcase_accept") != "1"]
    write_tsv(all_tsv, output_rows, fields)
    write_tsv(accepted_tsv, accepted, fields)
    write_tsv(still_tsv, still, fields)
    summary = {
        "rows": len(output_rows),
        "accepted": len(accepted),
        "still_unresolved": len(still),
        "accepted_by_confidence": dict(Counter(row.get("hardcase_confidence", "") for row in accepted)),
        "accepted_by_source": dict(Counter(row.get("hardcase_candidate_source", "") for row in accepted)),
        "still_by_final_decision": dict(Counter(row.get("final_decision", "") for row in still)),
        "all_tsv": str(all_tsv),
        "accepted_tsv": str(accepted_tsv),
        "still_tsv": str(still_tsv),
    }
    (args.output_dir / "hardcase_427_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
