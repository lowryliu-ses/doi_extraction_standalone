#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from doi_pipeline.doi import find_dois, is_plausible_doi, normalize_doi
from doi_pipeline.hardcase_postprocess import Authority, Cache, Candidate, Resolver, authority_title_coverage
from doi_pipeline.metadata import author_score, clean, norm_text, parse_authors, title_score
from doi_pipeline.ocr import ocr_pdf_front

try:
    from tools.resolve_remaining_143 import ExtendedResolver, normalize_doi_url
except Exception:
    from resolve_remaining_143 import ExtendedResolver, normalize_doi_url  # type: ignore

csv.field_size_limit(sys.maxsize)

BAD_TITLE_RE = re.compile(
    r"(?:^|\b)(?:abstract|keywords?|introduction|references|bibliography|table of contents|"
    r"copyright|downloaded from|non-commercial research|摘要|关键词|引言)(?:\b|[:：])",
    re.IGNORECASE,
)
REFERENCE_CONTEXT_RE = re.compile(
    r"\b(?:references|bibliography|copyright|downloaded|j\. immunol\.|submitted should|please make sure)\b",
    re.IGNORECASE,
)
SUSPICIOUS_DOI_RE = re.compile(
    r"(?:\.s\d{3,4}$|/fig(?:ure)?[-_/]?\d+|/suppl|supplement|supporting[-_]?information|dryad|zenodo)",
    re.IGNORECASE,
)


def useful_title(value: Any) -> bool:
    title = clean(value)
    normalized = norm_text(title)
    tokens = [token for token in normalized.split() if len(token) >= 3]
    return bool(title) and len(title) <= 280 and len(normalized) >= 18 and len(tokens) >= 4 and not BAD_TITLE_RE.search(title)


def row_pdf_path(row: dict[str, Any], local_base: Path | None) -> Path:
    if local_base:
        candidate = local_base / "files" / clean(row.get("folder")) / clean(row.get("filename"))
        if candidate.exists():
            return candidate
    return Path(clean(row.get("round2_pdf_path")) or clean(row.get("still_target_path")) or clean(row.get("target_path")) or clean(row.get("path")))


def existing_text_chars(row: dict[str, Any]) -> int:
    value = 0
    for field in ("round2_pdf_text_chars", "hardcase_pdf_text_chars"):
        try:
            value = max(value, int(float(row.get(field) or 0)))
        except Exception:
            pass
    return value


def regex_doi_candidates(text: str) -> list[tuple[str, str, str]]:
    hits: list[tuple[str, str, str]] = []
    for hit in find_dois(text, "ocr_regex", allow_spaced=True):
        hits.append((hit.doi, "ocr_regex", hit.context))
    out: list[tuple[str, str, str]] = []
    for doi, source, context in sorted(hits, key=lambda item: len(item[0]), reverse=True):
        lower = doi.lower()
        if any(existing.lower().startswith(lower + ".") or existing.lower().startswith(lower + "/") for existing, _, _ in out):
            continue
        if lower not in {existing.lower() for existing, _, _ in out}:
            out.append((doi, source, context))
    return out


def add_candidate(candidates: list[Candidate], seen: set[str], doi: str, source: str, reason: str) -> None:
    normalized = normalize_doi_url(doi)
    if not normalized or normalized.lower() in seen:
        return
    seen.add(normalized.lower())
    candidates.append(Candidate(normalized, source, reason))


def suspicious_candidate(candidate: Candidate, authority: Authority) -> bool:
    doi = normalize_doi_url(authority.authority_doi or authority.doi or candidate.doi)
    return bool(doi and SUSPICIOUS_DOI_RE.search(doi))


def evaluate_candidate(
    title: str,
    authors: list[str],
    text: str,
    candidate: Candidate,
    authority: Authority,
) -> dict[str, Any]:
    doi = normalize_doi_url(authority.authority_doi or authority.doi or candidate.doi)
    ts = title_score(title, authority.title)
    aus = author_score(authors, authority.authors)
    coverage = authority_title_coverage(authority.title, text)
    accepted = False
    confidence = "none"
    reason = "OCR evidence below threshold"

    if not authority.resolved or not authority.title:
        reason = authority.error or "authority metadata unavailable"
    elif suspicious_candidate(candidate, authority):
        reason = "candidate looks like supplement, dataset, component, or reference DOI"
    elif coverage >= 0.95 and not REFERENCE_CONTEXT_RE.search(candidate.reason):
        accepted, confidence = True, "medium"
        reason = f"OCR DOI visible and authority title appears in OCR text ({coverage:.3f}); OCR title extraction was weak"
    elif useful_title(title):
        if ts >= 0.985 and (not authors or aus >= 0.10 or coverage >= 0.70):
            accepted, confidence = True, "high"
            reason = f"OCR DOI visible and authority title near-exact ({ts:.3f}/{aus:.3f}/{coverage:.3f})"
        elif ts >= 0.94 and aus >= 0.15:
            accepted, confidence = True, "high"
            reason = f"OCR DOI visible and title/author match ({ts:.3f}/{aus:.3f})"
        elif coverage >= 0.90 and ts >= 0.80:
            accepted, confidence = True, "medium"
            reason = f"OCR DOI visible with strong OCR title support ({ts:.3f}/{coverage:.3f})"
    else:
        reason = "OCR title missing, too short, generic, or boilerplate"

    return {
        "ocr_accept": "1" if accepted else "0",
        "ocr_confidence": confidence,
        "ocr_reason": reason,
        "ocr_final_doi": doi if accepted else "",
        "ocr_candidate_doi": doi,
        "ocr_candidate_source": candidate.source,
        "ocr_candidate_reason": candidate.reason,
        "ocr_title_score": f"{ts:.4f}",
        "ocr_author_score": f"{aus:.4f}",
        "ocr_pdf_title_coverage": f"{coverage:.4f}",
        "ocr_authority_source": authority.source,
        "ocr_authority_doi": authority.authority_doi or authority.doi,
        "ocr_authority_title": authority.title,
        "ocr_authority_authors": "; ".join(authority.authors),
        "ocr_authority_container": authority.container,
        "ocr_authority_year": authority.year,
        "ocr_authority_type": authority.type,
        "ocr_authority_error": authority.error,
    }


def empty_result(reason: str) -> dict[str, Any]:
    return {
        "ocr_accept": "0",
        "ocr_confidence": "none",
        "ocr_reason": reason,
        "ocr_final_doi": "",
        "ocr_candidate_doi": "",
        "ocr_candidate_source": "",
        "ocr_candidate_reason": "",
        "ocr_title_score": "0.0000",
        "ocr_author_score": "0.0000",
        "ocr_pdf_title_coverage": "0.0000",
        "ocr_authority_source": "",
        "ocr_authority_doi": "",
        "ocr_authority_title": "",
        "ocr_authority_authors": "",
        "ocr_authority_container": "",
        "ocr_authority_year": "",
        "ocr_authority_type": "",
        "ocr_authority_error": "",
    }


def best_result(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return empty_result("no OCR DOI candidate")

    def rank(row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            row.get("ocr_accept") == "1",
            {"high": 3, "medium": 2, "low": 1, "none": 0}.get(row.get("ocr_confidence", ""), 0),
            float(row.get("ocr_title_score") or 0),
            float(row.get("ocr_author_score") or 0),
            float(row.get("ocr_pdf_title_coverage") or 0),
            len(row.get("ocr_candidate_doi") or ""),
        )

    return max(results, key=rank)


def process_row(row: dict[str, Any], resolver: Resolver, args: argparse.Namespace) -> dict[str, Any]:
    pdf_path = row_pdf_path(row, args.local_base)
    result = ocr_pdf_front(pdf_path, pages=args.ocr_pages, dpi=args.ocr_dpi, language=args.ocr_language) if pdf_path.exists() else None
    text = result.text if result else ""
    title = result.title if result else ""
    authors = result.authors if result else []
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for doi, source, reason in regex_doi_candidates(text):
        add_candidate(candidates, seen, doi, source, reason)
    results = [evaluate_candidate(title, authors, text, candidate, resolver.resolve(candidate.doi)) for candidate in candidates]
    best = best_result(results)
    return {
        **row,
        "ocr_extracted_title": title,
        "ocr_extracted_authors": "; ".join(authors),
        "ocr_text_chars": str(len(text)),
        "ocr_pages_read": str(result.pages_read if result else 0),
        "ocr_error": result.error if result else "missing PDF",
        "ocr_pdf_path": str(pdf_path),
        "ocr_candidates_checked": str(len(results)),
        **best,
    }


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve low-text PDFs with local OCR and DOI authority validation")
    parser.add_argument("input_tsv", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--local-base", type=Path)
    parser.add_argument("--all", action="store_true", help="OCR every row instead of only low-text rows")
    parser.add_argument("--max-existing-text-chars", type=int, default=500)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--ocr-pages", type=int, default=2)
    parser.add_argument("--ocr-dpi", type=int, default=300)
    parser.add_argument("--ocr-language", default="eng+chi_sim+jpn+deu+fra+por+rus")
    args = parser.parse_args(argv)

    rows = list(csv.DictReader(args.input_tsv.open(encoding="utf-8", errors="replace"), delimiter="\t"))
    targets = rows if args.all else [row for row in rows if existing_text_chars(row) < args.max_existing_text_chars]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    resolver = ExtendedResolver(args.output_dir / "ocr_resolver_cache.jsonl", args.timeout).base

    out_rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(process_row, row, resolver, args): row for row in targets}
        for index, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            out_rows.append(row)
            print(
                f"{index}/{len(targets)}\t{row.get('ocr_accept')}\t"
                f"{row.get('ocr_final_doi') or row.get('ocr_candidate_doi')}\t{row.get('filename')}",
                flush=True,
            )

    out_rows.sort(key=lambda row: (row.get("folder", ""), row.get("filename", "")))
    new_fields = [
        "ocr_accept",
        "ocr_confidence",
        "ocr_reason",
        "ocr_final_doi",
        "ocr_candidate_doi",
        "ocr_candidate_source",
        "ocr_candidate_reason",
        "ocr_title_score",
        "ocr_author_score",
        "ocr_pdf_title_coverage",
        "ocr_extracted_title",
        "ocr_extracted_authors",
        "ocr_authority_source",
        "ocr_authority_doi",
        "ocr_authority_title",
        "ocr_authority_authors",
        "ocr_authority_container",
        "ocr_authority_year",
        "ocr_authority_type",
        "ocr_authority_error",
        "ocr_text_chars",
        "ocr_pages_read",
        "ocr_error",
        "ocr_pdf_path",
        "ocr_candidates_checked",
    ]
    base_fields = list(rows[0].keys()) if rows else []
    fields = new_fields + [field for field in base_fields if field not in new_fields]
    accepted = [row for row in out_rows if row.get("ocr_accept") == "1"]
    still = [row for row in out_rows if row.get("ocr_accept") != "1"]
    with_candidate = [row for row in still if row.get("ocr_candidate_doi")]
    no_candidate = [row for row in still if not row.get("ocr_candidate_doi")]
    write_tsv(args.output_dir / "ocr_resolution.tsv", out_rows, fields)
    write_tsv(args.output_dir / "ocr_accepted.tsv", accepted, fields)
    write_tsv(args.output_dir / "ocr_still.tsv", still, fields)
    write_tsv(args.output_dir / "ocr_candidate_rejected.tsv", with_candidate, fields)
    write_tsv(args.output_dir / "ocr_no_candidate.tsv", no_candidate, fields)
    summary = {
        "input_rows": len(rows),
        "targets": len(targets),
        "accepted": len(accepted),
        "still": len(still),
        "candidate_rejected": len(with_candidate),
        "no_candidate": len(no_candidate),
        "accepted_by_confidence": dict(Counter(row.get("ocr_confidence", "") for row in accepted)),
        "accepted_by_source": dict(Counter(row.get("ocr_candidate_source", "") for row in accepted)),
        "still_by_reason": dict(Counter(row.get("ocr_reason", "") for row in still)),
        "ocr_errors": dict(Counter(row.get("ocr_error", "") for row in out_rows if row.get("ocr_error"))),
    }
    (args.output_dir / "ocr_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
