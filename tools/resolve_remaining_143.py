#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from doi_pipeline.hardcase_postprocess import (
    Authority,
    Cache,
    Candidate,
    Resolver,
    authority_title_coverage,
    best_title_from_pdf_text,
    build_candidates,
    decision_for,
    pdf_text,
    title_is_bad,
)
from doi_pipeline.metadata import author_score, clean, norm_text, parse_authors, title_score

csv.field_size_limit(sys.maxsize)


def normalize_doi_url(value: Any) -> str:
    from doi_pipeline.doi import is_plausible_doi, normalize_doi

    doi = normalize_doi(value)
    return doi if doi and is_plausible_doi(doi) else ""


def add_candidate(candidates: list[Candidate], seen: set[str], doi: str, source: str, reason: str) -> None:
    normalized = normalize_doi_url(doi)
    if not normalized:
        return
    key = normalized.lower()
    if key in seen:
        return
    seen.add(key)
    candidates.append(Candidate(normalized, source, reason))


def extra_prefix_variants(doi: str) -> list[Candidate]:
    doi = normalize_doi_url(doi)
    if not doi or "/" not in doi:
        return []
    prefix, suffix = doi.split("/", 1)
    lower_prefix = prefix.lower()
    variants: list[Candidate] = []
    seen: set[str] = set()

    def add(value: str, reason: str) -> None:
        add_candidate(variants, seen, value, "prefix_variant", reason)

    if lower_prefix == "10.1049" and "_" in suffix:
        head, tail = suffix.split("_", 1)
        add(f"{prefix}/{head}:{tail}", "IET underscore-to-colon DOI")
        if head.lower() == "ic":
            add(f"{prefix}/cp:{tail}", "IET conference publication DOI")
    if lower_prefix == "10.1023" and "_" in suffix:
        head, tail = suffix.split("_", 1)
        add(f"{prefix}/{head.upper()}:{tail}", "Springer legacy DOI")
    if lower_prefix == "10.4233" and suffix.lower().startswith("uuid_"):
        add(f"{prefix}/uuid:{suffix.split('_', 1)[1]}", "TU Delft UUID DOI")
    if lower_prefix == "10.1117" and suffix.startswith("12."):
        parts = suffix.split(".")
        if len(parts) > 2:
            add(f"{prefix}/{'.'.join(parts[:2])}", "SPIE DOI stripped media suffix")
    if "_" in suffix and lower_prefix in {"10.1209", "10.1088"}:
        add(f"{prefix}/{suffix.replace('_', '/')}", "journal underscore path DOI")
    return variants


def title_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"^clean_\d+_", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"10\.\d{4,9}[_@-][A-Za-z0-9][A-Za-z0-9._;()/:+@-]*", " ", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\(\d+\)$", " ", stem)
    stem = stem.replace("_", " ").replace("-", " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem if len(norm_text(stem)) >= 18 else ""


def title_queries(row: dict[str, Any], text: str) -> list[str]:
    queries: list[str] = []
    for value in (
        row.get("title"),
        row.get("hardcase_pdf_guessed_title"),
        best_title_from_pdf_text(text),
        title_from_filename(clean(row.get("filename"))),
    ):
        title = clean(value)
        if title and not title_is_bad(title) and len(norm_text(title)) >= 18:
            key = norm_text(title)
            if key not in {norm_text(item) for item in queries}:
                queries.append(title)
    return queries


def request_json(url: str, timeout: float, accept: str = "application/json") -> Any:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "doi-remaining143-resolver/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


class ExtendedResolver:
    def __init__(self, cache_path: Path, timeout: float) -> None:
        self.base = Resolver(cache_path, timeout)
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

    def resolve(self, doi: str) -> Authority:
        return self.base.resolve(doi)

    def crossref_bibliographic_search(self, query_text: str, rows: int = 8) -> list[Authority]:
        from doi_pipeline.hardcase_postprocess import parse_crossref

        query = urllib.parse.urlencode({"query.bibliographic": query_text, "rows": str(rows)})
        url = f"https://api.crossref.org/works?{query}"
        data = self.cached(f"crossref-biblio:{norm_text(query_text)}", lambda: request_json(url, self.timeout))
        items = (((data or {}).get("message") or {}).get("items") or []) if isinstance(data, dict) else []
        out: list[Authority] = []
        for item in items:
            if isinstance(item, dict) and item.get("DOI"):
                out.append(parse_crossref(normalize_doi_url(item.get("DOI")) or clean(item.get("DOI")), {"message": item}))
        return out

    def openalex_search(self, query_text: str, rows: int = 8) -> list[Authority]:
        query = urllib.parse.urlencode({"search": query_text, "per-page": str(rows)})
        url = f"https://api.openalex.org/works?{query}"
        data = self.cached(f"openalex:{norm_text(query_text)}", lambda: request_json(url, self.timeout))
        items = ((data or {}).get("results") or []) if isinstance(data, dict) else []
        out: list[Authority] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            doi = normalize_doi_url(item.get("doi") or item.get("doi_url") or "")
            if not doi:
                continue
            authors = []
            for authorship in item.get("authorships") or []:
                if isinstance(authorship, dict):
                    author = authorship.get("author") or {}
                    name = clean(author.get("display_name") if isinstance(author, dict) else "")
                    if name:
                        authors.append(name)
            source = ((item.get("primary_location") or {}).get("source") or {}) if isinstance(item.get("primary_location"), dict) else {}
            out.append(
                Authority(
                    True,
                    doi,
                    source="openalex",
                    authority_doi=doi,
                    title=clean(item.get("title") or item.get("display_name")),
                    authors=authors,
                    container=clean(source.get("display_name") if isinstance(source, dict) else ""),
                    publisher="OpenAlex",
                    year=clean(item.get("publication_year")),
                    type=clean(item.get("type")),
                )
            )
        return out

    def semantic_scholar_search(self, query_text: str, rows: int = 8) -> list[Authority]:
        query = urllib.parse.urlencode(
            {
                "query": query_text,
                "limit": str(rows),
                "fields": "title,authors,year,venue,externalIds,publicationTypes",
            }
        )
        url = f"https://api.semanticscholar.org/graph/v1/paper/search?{query}"
        data = self.cached(f"semantic:{norm_text(query_text)}", lambda: request_json(url, self.timeout))
        items = ((data or {}).get("data") or []) if isinstance(data, dict) else []
        out: list[Authority] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            external_ids = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
            doi = normalize_doi_url(external_ids.get("DOI") or "")
            if not doi:
                continue
            authors = [clean(author.get("name")) for author in item.get("authors", []) if isinstance(author, dict) and clean(author.get("name"))]
            out.append(
                Authority(
                    True,
                    doi,
                    source="semantic_scholar",
                    authority_doi=doi,
                    title=clean(item.get("title")),
                    authors=authors,
                    container=clean(item.get("venue")),
                    publisher="Semantic Scholar",
                    year=clean(item.get("year")),
                    type="; ".join(item.get("publicationTypes") or []),
                )
            )
        return out

    def title_search_all(self, query_text: str) -> list[tuple[Candidate, Authority]]:
        pairs: list[tuple[Candidate, Authority]] = []
        for source, fn in (
            ("crossref_bibliographic_search", self.crossref_bibliographic_search),
            ("openalex_title_search", self.openalex_search),
            ("semantic_scholar_title_search", self.semantic_scholar_search),
        ):
            for authority in fn(query_text):
                pairs.append((Candidate(authority.authority_doi or authority.doi, source, "extended title search"), authority))
        return pairs


def decision_for_extended(row: dict[str, Any], candidate: Candidate, authority: Authority, text: str, query_title: str = "") -> dict[str, Any]:
    eval_row = row
    if query_title and (title_is_bad(row.get("title")) or title_score(row.get("title"), query_title) < 0.6):
        eval_row = {**row, "title": query_title}
    result = decision_for(eval_row, candidate, authority, text)

    local_title = clean(eval_row.get("title"))
    local_authors = parse_authors(eval_row.get("authors"))
    ts = title_score(local_title, authority.title)
    aus = author_score(local_authors, authority.authors)
    coverage = authority_title_coverage(authority.title, text)

    accepted = result["hardcase_accept"] == "1"
    confidence = result["hardcase_confidence"]
    reason = result["hardcase_reason"]

    if not accepted and authority.resolved:
        if ts >= 0.93 and (aus >= 0.10 or not local_authors):
            accepted, confidence, reason = True, "high", f"extended title/author authority match ({ts:.3f}/{aus:.3f})"
        elif ts >= 0.86 and aus >= 0.50:
            accepted, confidence, reason = True, "medium", f"extended title search with author support ({ts:.3f}/{aus:.3f})"
        elif coverage >= 0.72 and len(norm_text(authority.title).split()) >= 4 and candidate.source == "prefix_variant":
            accepted, confidence, reason = True, "medium", f"prefix DOI variant title appears in PDF text ({coverage:.3f})"

    result.update(
        {
            "round2_accept": "1" if accepted else "0",
            "round2_confidence": confidence,
            "round2_reason": reason,
            "round2_final_doi": (authority.authority_doi or candidate.doi) if accepted else "",
            "round2_candidate_doi": candidate.doi,
            "round2_candidate_source": candidate.source,
            "round2_candidate_reason": candidate.reason,
            "round2_title_score": f"{ts:.4f}",
            "round2_author_score": f"{aus:.4f}",
            "round2_pdf_title_coverage": f"{coverage:.4f}",
            "round2_query_title": query_title,
            "round2_authority_source": authority.source,
            "round2_authority_doi": authority.authority_doi,
            "round2_authority_title": authority.title,
            "round2_authority_authors": "; ".join(authority.authors),
            "round2_authority_container": authority.container,
            "round2_authority_year": authority.year,
            "round2_authority_error": authority.error,
        }
    )
    return result


def row_pdf_path(row: dict[str, Any], local_base: Path | None) -> Path:
    if local_base:
        candidate = local_base / "files" / clean(row.get("folder")) / clean(row.get("filename"))
        if candidate.exists():
            return candidate
    return Path(clean(row.get("still_target_path")) or clean(row.get("target_path")) or clean(row.get("path")))


def process_row(row: dict[str, Any], resolver: ExtendedResolver, pages: int, local_base: Path | None) -> dict[str, Any]:
    path = row_pdf_path(row, local_base)
    text = pdf_text(path, pages) if path.exists() else ""
    candidates = build_candidates(row)
    seen = {candidate.doi.lower() for candidate in candidates}
    for value in (row.get("doi"), row.get("hardcase_candidate_doi"), row.get("filename")):
        for candidate in extra_prefix_variants(clean(value)):
            add_candidate(candidates, seen, candidate.doi, candidate.source, candidate.reason)

    results: list[dict[str, Any]] = []
    tried: list[str] = []
    for candidate in candidates:
        tried.append(candidate.doi)
        authority = resolver.resolve(candidate.doi)
        results.append(decision_for_extended(row, candidate, authority, text))

    for query in title_queries(row, text):
        for candidate, authority in resolver.title_search_all(query):
            results.append(decision_for_extended(row, candidate, authority, text, query_title=query))

    best: dict[str, Any] | None = None
    for result in results:
        rank = (
            result["round2_accept"] == "1",
            {"high": 3, "medium": 2, "low": 1, "none": 0}.get(result["round2_confidence"], 0),
            float(result["round2_title_score"]),
            float(result["round2_pdf_title_coverage"]),
            float(result["round2_author_score"]),
        )
        if best is None:
            best = result
            continue
        best_rank = (
            best["round2_accept"] == "1",
            {"high": 3, "medium": 2, "low": 1, "none": 0}.get(best["round2_confidence"], 0),
            float(best["round2_title_score"]),
            float(best["round2_pdf_title_coverage"]),
            float(best["round2_author_score"]),
        )
        if rank > best_rank:
            best = result

    if best is None:
        best = {
            "round2_accept": "0",
            "round2_confidence": "none",
            "round2_reason": "no candidate or title-search DOI verified",
            "round2_final_doi": "",
            "round2_candidate_doi": "",
            "round2_candidate_source": "",
            "round2_candidate_reason": "",
            "round2_title_score": "0.0000",
            "round2_author_score": "0.0000",
            "round2_pdf_title_coverage": "0.0000",
            "round2_query_title": "",
            "round2_authority_source": "",
            "round2_authority_doi": "",
            "round2_authority_title": "",
            "round2_authority_authors": "",
            "round2_authority_container": "",
            "round2_authority_year": "",
            "round2_authority_error": "",
        }
    return {**row, **best, "round2_tried_dois": "; ".join(dict.fromkeys(tried)), "round2_pdf_text_chars": str(len(text)), "round2_pdf_path": str(path)}


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second-round resolver for the remaining 143 DOI hard cases")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--local-base", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=18.0)
    parser.add_argument("--pdf-pages", type=int, default=10)
    args = parser.parse_args(argv)

    rows = list(csv.DictReader(args.manifest.open(encoding="utf-8", errors="replace"), delimiter="\t"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    resolver = ExtendedResolver(args.output_dir / "remaining143_cache.jsonl", args.timeout)

    out_rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(process_row, row, resolver, args.pdf_pages, args.local_base): row for row in rows}
        for index, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            out_rows.append(row)
            print(f"{index}/{len(rows)}\t{row.get('round2_accept')}\t{row.get('round2_final_doi') or row.get('round2_candidate_doi')}\t{row.get('filename')}", flush=True)

    out_rows.sort(key=lambda row: (row.get("folder", ""), row.get("filename", "")))
    new_fields = [
        "round2_accept",
        "round2_confidence",
        "round2_reason",
        "round2_final_doi",
        "round2_candidate_doi",
        "round2_candidate_source",
        "round2_candidate_reason",
        "round2_title_score",
        "round2_author_score",
        "round2_pdf_title_coverage",
        "round2_query_title",
        "round2_authority_source",
        "round2_authority_doi",
        "round2_authority_title",
        "round2_authority_authors",
        "round2_authority_container",
        "round2_authority_year",
        "round2_authority_error",
        "round2_tried_dois",
        "round2_pdf_text_chars",
        "round2_pdf_path",
    ]
    base_fields = list(rows[0].keys()) if rows else []
    fields = new_fields + [field for field in base_fields if field not in new_fields]
    accepted = [row for row in out_rows if row.get("round2_accept") == "1"]
    still = [row for row in out_rows if row.get("round2_accept") != "1"]
    write_tsv(args.output_dir / "remaining143_round2_resolution.tsv", out_rows, fields)
    write_tsv(args.output_dir / "remaining143_round2_accepted.tsv", accepted, fields)
    write_tsv(args.output_dir / "remaining143_round2_still.tsv", still, fields)
    summary = {
        "rows": len(out_rows),
        "accepted": len(accepted),
        "still": len(still),
        "accepted_by_confidence": dict(Counter(row.get("round2_confidence", "") for row in accepted)),
        "accepted_by_source": dict(Counter(row.get("round2_candidate_source", "") for row in accepted)),
        "accepted_by_authority": dict(Counter(row.get("round2_authority_source", "") for row in accepted)),
        "still_by_final_decision": dict(Counter(row.get("final_decision", "") for row in still)),
    }
    (args.output_dir / "remaining143_round2_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
