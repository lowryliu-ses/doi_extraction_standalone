#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from doi_pipeline.doi import find_dois, is_plausible_doi, normalize_doi
from doi_pipeline.hardcase_postprocess import Authority, Cache, Candidate, authority_title_coverage, pdf_text
from doi_pipeline.metadata import author_score, clean, norm_text, parse_authors, title_score
from doi_pipeline.ocr import extract_title_authors_from_text

try:
    from tools.resolve_remaining_143 import ExtendedResolver, normalize_doi_url
except Exception:
    from resolve_remaining_143 import ExtendedResolver, normalize_doi_url  # type: ignore

csv.field_size_limit(sys.maxsize)

STOP_TITLE_TOKENS = {
    "battery",
    "batteries",
    "electrode",
    "electrodes",
    "electrolyte",
    "electrolytes",
    "lithium",
    "sodium",
    "materials",
    "technologies",
    "chapter",
    "introduction",
    "review",
}

BAD_TITLE_RE = re.compile(
    r"(?:^|\b)(?:abstract|keywords?|introduction|references|bibliography|table of contents|"
    r"preparing your manuscript|contents programmed|new titles for|titles are limited|"
    r"downloaded from|copyright|non-commercial research|摘要|关键词|引言)(?:\b|[:：])",
    re.IGNORECASE,
)
SUSPICIOUS_DOI_RE = re.compile(
    r"(?:\.s\d{3,4}$|/fig(?:ure)?[-_/]?\d+|/suppl|supplement|supporting[-_]?information|dryad|zenodo)",
    re.IGNORECASE,
)
SUSPICIOUS_TITLE_RE = re.compile(
    r"\b(?:supporting information|supplementary material|figure \d+|table \d+|dataset|data from:)\b",
    re.IGNORECASE,
)
REFERENCE_CONTEXT_RE = re.compile(
    r"\b(?:references|bibliography|ref\.|j\. immunol\.|copyright release|after the acceptance|"
    r"acceptance of the paper|submitted should|please make sure)\b",
    re.IGNORECASE,
)


def useful_title(value: Any) -> bool:
    title = clean(value)
    normalized = norm_text(title)
    tokens = [token for token in normalized.split() if len(token) >= 3]
    informative = [token for token in tokens if token not in STOP_TITLE_TOKENS]
    return (
        bool(title)
        and len(title) <= 280
        and len(normalized) >= 18
        and len(tokens) >= 4
        and len(informative) >= 2
        and not BAD_TITLE_RE.search(title)
    )


def row_pdf_path(row: dict[str, Any], local_base: Path | None) -> Path:
    if local_base:
        candidate = local_base / "files" / clean(row.get("folder")) / clean(row.get("filename"))
        if candidate.exists():
            return candidate
    return Path(clean(row.get("content_pdf_path")) or clean(row.get("round2_pdf_path")) or clean(row.get("still_target_path")) or clean(row.get("target_path")) or clean(row.get("path")))


def normalize_line(line: str) -> str:
    line = clean(line.replace("\x0c", " "))
    line = re.sub(r"\s+", " ", line)
    return line.strip()


def markdown_from_pdf(path: Path, pages: int, max_chars: int) -> tuple[str, str]:
    try:
        import fitz  # type: ignore
    except Exception:
        text = pdf_text(path, pages)
        return text[:max_chars], "pdf_text"

    chunks: list[str] = []
    try:
        with fitz.open(str(path)) as doc:
            for page_index in range(min(max(1, pages), len(doc))):
                page = doc[page_index]
                chunks.append(f"# Page {page_index + 1}")
                blocks = page.get_text("blocks") or []
                blocks = sorted(blocks, key=lambda block: (round(float(block[1]), 1), round(float(block[0]), 1)))
                for block in blocks:
                    raw = block[4] if len(block) >= 5 else ""
                    lines = [normalize_line(line) for line in str(raw).splitlines()]
                    lines = [line for line in lines if line]
                    if not lines:
                        continue
                    paragraph = " ".join(lines)
                    if len(paragraph) <= 3 or re.fullmatch(r"\d+", paragraph):
                        continue
                    chunks.append(paragraph)
                chunks.append("")
        return "\n\n".join(chunks)[:max_chars], "fitz_blocks"
    except Exception:
        text = pdf_text(path, pages)
        return text[:max_chars], "pdf_text_fallback"


def extract_json_object(text: str) -> dict[str, Any]:
    text = clean(text)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    return {}


def llm_endpoint(args: argparse.Namespace) -> str:
    if args.llm_url:
        return args.llm_url
    return args.llm_base_url.rstrip("/") + "/chat/completions"


def call_llm(markdown: str, args: argparse.Namespace, cache: Cache) -> tuple[dict[str, Any], str, str]:
    key = "llm-md:" + hashlib.sha256((args.llm_model + "\n" + markdown).encode("utf-8", errors="ignore")).hexdigest()
    cached = cache.get(key)
    if isinstance(cached, dict):
        return cached.get("parsed", {}), clean(cached.get("content")), clean(cached.get("error"))

    prompt = {
        "task": "Extract scholarly PDF metadata from the provided markdown text only.",
        "rules": [
            "Return strict JSON only. Do not wrap in markdown.",
            "Use only the provided markdown text. Do not infer from filename.",
            "The title must be the actual work title, not a journal name, publisher template, section heading, reference title, or table of contents item.",
            "Authors must be the work authors only. Do not include affiliations.",
            "DOI candidates must appear in the markdown text, including line-broken or spaced DOI forms. Do not invent a DOI.",
            "If a DOI appears only in references or for another work, set is_main_article=false.",
        ],
        "schema": {
            "title": "string",
            "authors": ["string"],
            "doi_candidates": [
                {
                    "doi": "10.xxxx/...",
                    "is_main_article": True,
                    "confidence": "high|medium|low",
                    "evidence": "short exact text evidence",
                }
            ],
            "year": "string",
            "document_type": "article|thesis|book_chapter|report|supplement|unknown",
            "notes": "short string",
        },
        "markdown": markdown,
    }
    payload = {
        "model": args.llm_model,
        "messages": [
            {"role": "system", "content": "You are a careful scholarly PDF metadata extractor. Return strict JSON only."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "temperature": 0,
    }
    headers = {"Content-Type": "application/json"}
    if args.llm_api_key:
        headers["Authorization"] = f"Bearer {args.llm_api_key}"
    request = urllib.request.Request(
        llm_endpoint(args),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    error = ""
    content = ""
    parsed: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(request, timeout=args.llm_timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        content = clean((((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""))
        parsed = extract_json_object(content)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    cache.set(key, {"parsed": parsed, "content": content, "error": error})
    time.sleep(args.llm_delay)
    return parsed, content, error


def parsed_authors(value: Any) -> list[str]:
    if isinstance(value, list):
        return [clean(item) for item in value if clean(item)]
    return parse_authors(value)


def llm_doi_candidates(parsed: dict[str, Any]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    raw = parsed.get("doi_candidates")
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            doi = normalize_doi(item.get("doi"))
            is_main = item.get("is_main_article")
            confidence = clean(item.get("confidence")).lower()
            evidence = clean(item.get("evidence"))
            if doi and is_plausible_doi(doi) and is_main is not False and confidence != "low":
                out.append((doi, "llm_markdown_doi", evidence))
        else:
            doi = normalize_doi(item)
            if doi and is_plausible_doi(doi):
                out.append((doi, "llm_markdown_doi", "LLM extracted DOI"))
    return out


def regex_doi_candidates(markdown: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for hit in find_dois(markdown[:16000], "markdown_regex", allow_spaced=True):
        out.append((hit.doi, "markdown_regex", hit.context))
    filtered: list[tuple[str, str, str]] = []
    for doi, source, reason in sorted(out, key=lambda item: len(item[0]), reverse=True):
        lower = doi.lower()
        if any(existing.lower().startswith(lower + ".") or existing.lower().startswith(lower + "/") for existing, _, _ in filtered):
            continue
        if lower not in {existing.lower() for existing, _, _ in filtered}:
            filtered.append((doi, source, reason))
    return filtered


def add_candidate(candidates: list[Candidate], seen: set[str], doi: str, source: str, reason: str) -> None:
    normalized = normalize_doi_url(doi)
    if not normalized or normalized.lower() in seen:
        return
    seen.add(normalized.lower())
    candidates.append(Candidate(normalized, source, reason))


def search_candidates(resolver: ExtendedResolver, title: str, authors: list[str], args: argparse.Namespace) -> list[tuple[Candidate, Authority, str]]:
    if not args.title_search or not useful_title(title):
        return []
    queries = [title]
    if authors:
        first = authors[0].split()[-1] if authors[0].split() else authors[0]
        if first:
            queries.append(f"{title} {first}")
    pairs: list[tuple[Candidate, Authority, str]] = []
    seen: set[str] = set()

    def add(authority: Authority, source: str, query: str) -> None:
        doi = normalize_doi_url(authority.authority_doi or authority.doi)
        if not doi or doi.lower() in seen:
            return
        seen.add(doi.lower())
        pairs.append((Candidate(doi, source, "LLM title search"), authority, query))

    for query in queries:
        for authority in resolver.base.crossref_title_search(query, rows=args.title_search_rows):
            add(authority, "llm_crossref_title_search", query)
        for authority in resolver.base.datacite_title_search(query, rows=args.title_search_rows):
            add(authority, "llm_datacite_title_search", query)
        for candidate, authority in resolver.title_search_all(query):
            add(authority, "llm_" + candidate.source, query)
    return pairs


def suspicious_candidate(candidate: Candidate, authority: Authority) -> bool:
    doi = normalize_doi_url(authority.authority_doi or authority.doi or candidate.doi)
    return bool(
        (doi and SUSPICIOUS_DOI_RE.search(doi))
        or SUSPICIOUS_TITLE_RE.search(authority.title)
        or norm_text(authority.type) in {"dataset", "posted content", "component"}
    )


def evaluate_candidate(
    title: str,
    authors: list[str],
    markdown: str,
    candidate: Candidate,
    authority: Authority,
    query: str,
) -> dict[str, Any]:
    doi = normalize_doi_url(authority.authority_doi or authority.doi or candidate.doi)
    ts = title_score(title, authority.title)
    aus = author_score(authors, authority.authors)
    coverage = authority_title_coverage(authority.title, markdown)
    doi_visible = candidate.source in {"markdown_regex", "llm_markdown_doi"}
    title_search = candidate.source.startswith("llm_") and "title_search" in candidate.source
    accepted = False
    confidence = "none"
    reason = "LLM markdown evidence below threshold"

    if not title or not useful_title(title):
        if doi_visible and coverage >= 0.95 and not REFERENCE_CONTEXT_RE.search(candidate.reason):
            accepted, confidence = True, "medium"
            reason = f"DOI visible in markdown and authority title appears in PDF text ({coverage:.3f}); local title extraction was weak"
        else:
            reason = "LLM title missing, too short, generic, or boilerplate"
    elif not authority.resolved or not authority.title:
        reason = authority.error or "authority metadata unavailable"
    elif suspicious_candidate(candidate, authority):
        reason = "candidate looks like supplement, dataset, component, or reference DOI"
    elif doi_visible and ts >= 0.985 and (not authors or aus >= 0.10 or coverage >= 0.70):
        accepted, confidence = True, "high"
        reason = f"DOI visible in markdown and authority title near-exact ({ts:.3f}/{aus:.3f}/{coverage:.3f})"
    elif doi_visible and ts >= 0.94 and aus >= 0.15:
        accepted, confidence = True, "high"
        reason = f"DOI visible in markdown and title/author match ({ts:.3f}/{aus:.3f})"
    elif doi_visible and coverage >= 0.90 and ts >= 0.85:
        accepted, confidence = True, "medium"
        reason = f"DOI visible in markdown with strong PDF title support ({ts:.3f}/{coverage:.3f})"
    elif title_search and ts >= 0.985 and (not authors or aus >= 0.10):
        accepted, confidence = True, "high"
        reason = f"LLM title search near-exact authority match ({ts:.3f}/{aus:.3f})"
    elif title_search and ts >= 0.94 and aus >= 0.50:
        accepted, confidence = True, "medium"
        reason = f"LLM title search has author support ({ts:.3f}/{aus:.3f})"

    return {
        "llm_md_accept": "1" if accepted else "0",
        "llm_md_confidence": confidence,
        "llm_md_reason": reason,
        "llm_md_final_doi": doi if accepted else "",
        "llm_md_candidate_doi": doi,
        "llm_md_candidate_source": candidate.source,
        "llm_md_candidate_reason": candidate.reason,
        "llm_md_query": query,
        "llm_md_title_score": f"{ts:.4f}",
        "llm_md_author_score": f"{aus:.4f}",
        "llm_md_pdf_title_coverage": f"{coverage:.4f}",
        "llm_md_authority_source": authority.source,
        "llm_md_authority_doi": authority.authority_doi or authority.doi,
        "llm_md_authority_title": authority.title,
        "llm_md_authority_authors": "; ".join(authority.authors),
        "llm_md_authority_container": authority.container,
        "llm_md_authority_year": authority.year,
        "llm_md_authority_type": authority.type,
        "llm_md_authority_error": authority.error,
    }


def empty_result(reason: str) -> dict[str, Any]:
    return {
        "llm_md_accept": "0",
        "llm_md_confidence": "none",
        "llm_md_reason": reason,
        "llm_md_final_doi": "",
        "llm_md_candidate_doi": "",
        "llm_md_candidate_source": "",
        "llm_md_candidate_reason": "",
        "llm_md_query": "",
        "llm_md_title_score": "0.0000",
        "llm_md_author_score": "0.0000",
        "llm_md_pdf_title_coverage": "0.0000",
        "llm_md_authority_source": "",
        "llm_md_authority_doi": "",
        "llm_md_authority_title": "",
        "llm_md_authority_authors": "",
        "llm_md_authority_container": "",
        "llm_md_authority_year": "",
        "llm_md_authority_type": "",
        "llm_md_authority_error": "",
    }


def best_result(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return empty_result("no LLM markdown DOI or title-search candidate")

    def rank(row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            row.get("llm_md_accept") == "1",
            {"high": 3, "medium": 2, "low": 1, "none": 0}.get(row.get("llm_md_confidence", ""), 0),
            float(row.get("llm_md_title_score") or 0),
            float(row.get("llm_md_author_score") or 0),
            float(row.get("llm_md_pdf_title_coverage") or 0),
        )

    return max(results, key=rank)


def process_row(row: dict[str, Any], resolver: ExtendedResolver, llm_cache: Cache, args: argparse.Namespace) -> dict[str, Any]:
    pdf_path = row_pdf_path(row, args.local_base)
    markdown, markdown_source = markdown_from_pdf(pdf_path, args.pdf_pages, args.max_chars) if pdf_path.exists() else ("", "")
    if markdown and args.use_llm:
        parsed, raw_content, llm_error = call_llm(markdown, args, llm_cache)
    elif markdown:
        local_title, local_authors = extract_title_authors_from_text(markdown)
        parsed = {
            "title": local_title or clean(row.get("content_title") or row.get("title")),
            "authors": local_authors or parse_authors(row.get("content_authors") or row.get("authors")),
            "doi_candidates": [],
            "document_type": "",
            "notes": "LLM disabled; local markdown heuristic extraction only",
        }
        raw_content = ""
        llm_error = "llm disabled"
    else:
        parsed, raw_content, llm_error = {}, "", "missing PDF markdown text"
    llm_title = clean(parsed.get("title"))
    llm_authors = parsed_authors(parsed.get("authors"))
    llm_year = clean(parsed.get("year"))
    llm_document_type = clean(parsed.get("document_type"))

    candidates: list[Candidate] = []
    seen: set[str] = set()
    for doi, source, reason in regex_doi_candidates(markdown):
        add_candidate(candidates, seen, doi, source, reason)
    for doi, source, reason in llm_doi_candidates(parsed):
        add_candidate(candidates, seen, doi, source, reason)

    results: list[dict[str, Any]] = []
    for candidate in candidates:
        authority = resolver.resolve(candidate.doi)
        results.append(evaluate_candidate(llm_title, llm_authors, markdown, candidate, authority, "visible_doi"))

    for candidate, authority, query in search_candidates(resolver, llm_title, llm_authors, args):
        results.append(evaluate_candidate(llm_title, llm_authors, markdown, candidate, authority, query))

    best = best_result(results)
    return {
        **row,
        "llm_md_title": llm_title,
        "llm_md_authors": "; ".join(llm_authors),
        "llm_md_year": llm_year,
        "llm_md_document_type": llm_document_type,
        "llm_md_notes": clean(parsed.get("notes")),
        "llm_md_raw_doi_candidates": json.dumps(parsed.get("doi_candidates", []), ensure_ascii=False, separators=(",", ":")),
        "llm_md_error": llm_error,
        "llm_md_raw_response": raw_content[:4000],
        "llm_md_markdown_source": markdown_source,
        "llm_md_markdown_chars": str(len(markdown)),
        "llm_md_pdf_path": str(pdf_path),
        "llm_md_candidates_checked": str(len(results)),
        **best,
    }


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve hard PDFs by PDF-to-markdown plus OpenAI-compatible LLM metadata extraction")
    parser.add_argument("input_tsv", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--local-base", type=Path)
    parser.add_argument("--filter-decision", default="", help="Optional final_decision filter, for example conflict_review")
    parser.add_argument("--llm-url", default="http://160.72.54.193/v1/chat/completions")
    parser.add_argument("--llm-base-url", default="")
    parser.add_argument("--llm-model", default="qwen3-235b-a22b-instruct-2507")
    parser.add_argument("--llm-api-key", default="")
    parser.add_argument("--llm-timeout", type=float, default=120.0)
    parser.add_argument("--llm-delay", type=float, default=0.02)
    parser.add_argument("--no-llm", dest="use_llm", action="store_false", help="Do not call the model; use local markdown heuristics and DOI regex only")
    parser.set_defaults(use_llm=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=24.0)
    parser.add_argument("--pdf-pages", type=int, default=6)
    parser.add_argument("--max-chars", type=int, default=12000)
    parser.add_argument("--title-search", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--title-search-rows", type=int, default=5)
    args = parser.parse_args(argv)

    rows = list(csv.DictReader(args.input_tsv.open(encoding="utf-8", errors="replace"), delimiter="\t"))
    targets = [row for row in rows if not args.filter_decision or clean(row.get("final_decision")) == args.filter_decision]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    resolver = ExtendedResolver(args.output_dir / "llm_markdown_resolver_cache.jsonl", args.timeout)
    llm_cache = Cache(args.output_dir / "llm_markdown_cache.jsonl")

    out_rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(process_row, row, resolver, llm_cache, args): row for row in targets}
        for index, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            out_rows.append(row)
            print(
                f"{index}/{len(targets)}\t{row.get('llm_md_accept')}\t"
                f"{row.get('llm_md_final_doi') or row.get('llm_md_candidate_doi')}\t{row.get('filename')}",
                flush=True,
            )

    out_rows.sort(key=lambda row: (row.get("folder", ""), row.get("filename", "")))
    new_fields = [
        "llm_md_accept",
        "llm_md_confidence",
        "llm_md_reason",
        "llm_md_final_doi",
        "llm_md_candidate_doi",
        "llm_md_candidate_source",
        "llm_md_candidate_reason",
        "llm_md_query",
        "llm_md_title_score",
        "llm_md_author_score",
        "llm_md_pdf_title_coverage",
        "llm_md_title",
        "llm_md_authors",
        "llm_md_year",
        "llm_md_document_type",
        "llm_md_notes",
        "llm_md_raw_doi_candidates",
        "llm_md_authority_source",
        "llm_md_authority_doi",
        "llm_md_authority_title",
        "llm_md_authority_authors",
        "llm_md_authority_container",
        "llm_md_authority_year",
        "llm_md_authority_type",
        "llm_md_authority_error",
        "llm_md_error",
        "llm_md_raw_response",
        "llm_md_markdown_source",
        "llm_md_markdown_chars",
        "llm_md_pdf_path",
        "llm_md_candidates_checked",
    ]
    base_fields = list(rows[0].keys()) if rows else []
    fields = new_fields + [field for field in base_fields if field not in new_fields]
    accepted = [row for row in out_rows if row.get("llm_md_accept") == "1"]
    still = [row for row in out_rows if row.get("llm_md_accept") != "1"]
    with_candidate = [row for row in still if row.get("llm_md_candidate_doi")]
    no_candidate = [row for row in still if not row.get("llm_md_candidate_doi")]
    write_tsv(args.output_dir / "llm_markdown_resolution.tsv", out_rows, fields)
    write_tsv(args.output_dir / "llm_markdown_accepted.tsv", accepted, fields)
    write_tsv(args.output_dir / "llm_markdown_still.tsv", still, fields)
    write_tsv(args.output_dir / "llm_markdown_candidate_rejected.tsv", with_candidate, fields)
    write_tsv(args.output_dir / "llm_markdown_no_candidate.tsv", no_candidate, fields)
    summary = {
        "input_rows": len(rows),
        "targets": len(targets),
        "accepted": len(accepted),
        "still": len(still),
        "candidate_rejected": len(with_candidate),
        "no_candidate": len(no_candidate),
        "accepted_by_confidence": dict(Counter(row.get("llm_md_confidence", "") for row in accepted)),
        "accepted_by_source": dict(Counter(row.get("llm_md_candidate_source", "") for row in accepted)),
        "accepted_by_document_type": dict(Counter(row.get("llm_md_document_type", "") for row in accepted)),
        "still_by_reason": dict(Counter(row.get("llm_md_reason", "") for row in still)),
        "title_nonempty": sum(1 for row in out_rows if row.get("llm_md_title")),
        "authors_nonempty": sum(1 for row in out_rows if row.get("llm_md_authors")),
        "llm_errors": dict(Counter(row.get("llm_md_error", "") for row in out_rows if row.get("llm_md_error"))),
    }
    (args.output_dir / "llm_markdown_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
