from __future__ import annotations

import argparse
import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .doi import DoiCandidate, extract_markdown_hits, extract_pdf_hits, hits_to_candidates, is_plausible_doi, normalize_doi, choose_candidate
from .grobid import process_header_document
from .hardcase_postprocess import HARDCASE_FIELDS, postprocess_hardcase_records
from .metadata import clean, is_bad_title, load_grobid_metadata, merge_grobid, parse_authors
from .ocr import OcrResult, ocr_pdf_front
from .review_postprocess import postprocess_review_records
from .validators import MetadataValidator

SUMMARY_FIELDS = [
    "filename",
    "path",
    "status",
    "method",
    "doi",
    "confidence",
    "doi_decision",
    "doi_source",
    "metadata_source",
    "title",
    "authors",
    "year",
    "journal",
    "publisher",
    "abstract",
    "grobid_doi",
    "grobid_title",
    "grobid_authors",
    "ocr_title",
    "ocr_authors",
    "ocr_error",
    "title_source",
    "authors_source",
    "doi_validation_source",
    "doi_validation_score",
    "original_status",
    "original_method",
    "original_doi",
    "original_confidence",
    "review_stage",
    "postprocess_decision",
    "postprocess_confidence",
    "postprocess_reason",
    "authority_doi",
    "authority_title",
    "authority_authors",
    "authority_container",
    "authority_publisher",
    "authority_type",
    "authority_year",
    "authority_title_score",
    "authority_author_score",
    "authority_error",
    "corrected_doi",
    "tried_dois",
    *HARDCASE_FIELDS,
    "doi_candidates",
    "error",
]


def flatten(value: Any) -> str:
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return "; ".join(str(item) for item in value)
    if value is None:
        return ""
    return str(value)


def maybe_markdown_hits(pdf_path: Path, markdown_dir: Path | None) -> list[DoiCandidate]:
    if not markdown_dir:
        return []
    candidates = [
        markdown_dir / f"{pdf_path.stem}.md",
        markdown_dir / f"{pdf_path.name}.md",
    ]
    for path in candidates:
        if path.exists():
            return hits_to_candidates(extract_markdown_hits(path.read_text(encoding="utf-8", errors="ignore")))
    return []


def append_error(record: dict[str, Any], message: str) -> None:
    if not message:
        return
    if record.get("error"):
        record["error"] = f"{record['error']} | {message}"
    else:
        record["error"] = message


def llm_candidates(record: dict[str, Any], snippets: str, args: argparse.Namespace) -> list[DoiCandidate]:
    if not args.llm_base_url or not args.llm_model or not args.llm_api_key:
        return []
    import urllib.request

    prompt = {
        "title": record.get("title", ""),
        "authors": record.get("authors", []),
        "text": snippets[:5000],
        "instruction": "Return strict JSON {\"candidates\":[\"10.xxxx/...\"]}. Only DOI candidates for the main article.",
    }
    payload = {
        "model": args.llm_model,
        "messages": [
            {"role": "system", "content": "You are a careful DOI candidate extractor. Return only JSON."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        args.llm_base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {args.llm_api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=args.llm_timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "{}")
        parsed = json.loads(content)
    except Exception:
        return []
    out: list[DoiCandidate] = []
    for candidate in parsed.get("candidates", []) if isinstance(parsed, dict) else []:
        doi = normalize_doi(candidate)
        if doi and is_plausible_doi(doi):
            out.append(DoiCandidate(doi, "llm_candidate", "llm", "review", "LLM candidate"))
    return out


def process_pdf(pdf_path: Path, args: argparse.Namespace, grobid_by_name: dict[str, dict[str, str]], validator: MetadataValidator | None) -> dict[str, Any]:
    record: dict[str, Any] = {"filename": pdf_path.name, "path": str(pdf_path), "error": ""}
    candidates: list[DoiCandidate] = []
    snippets = ""
    try:
        ocr_result: OcrResult | None = None
        ocr_candidates_added = False

        def ensure_ocr() -> OcrResult:
            nonlocal ocr_result
            if ocr_result is None:
                ocr_result = ocr_pdf_front(pdf_path, pages=args.ocr_pages, language=args.ocr_language)
                record["ocr_title"] = ocr_result.title
                record["ocr_authors"] = ocr_result.authors
                record["ocr_error"] = ocr_result.error
                append_error(record, ocr_result.error)
            return ocr_result

        def add_ocr_candidates() -> None:
            nonlocal ocr_candidates_added
            result = ensure_ocr()
            if not ocr_candidates_added and result.text:
                candidates.extend(hits_to_candidates(extract_markdown_hits(result.text, "ocr")))
                ocr_candidates_added = True

        hits, pdf_metadata, snippets = extract_pdf_hits(pdf_path, fast=args.fast)
        candidates.extend(hits_to_candidates(hits))
        grobid = grobid_by_name.get(pdf_path.name.lower())
        grobid_error = ""
        if args.grobid_on_missing and args.grobid_endpoint and (
            not grobid or not grobid.get("title") or not grobid.get("authors")
        ):
            try:
                fetched_grobid = process_header_document(pdf_path, args.grobid_endpoint, timeout=args.grobid_timeout)
                if fetched_grobid.get("title") or fetched_grobid.get("authors") or fetched_grobid.get("doi"):
                    grobid = {**(grobid or {}), **{key: value for key, value in fetched_grobid.items() if value}}
            except Exception as exc:
                grobid_error = f"grobid unavailable: {type(exc).__name__}: {exc}"
        grobid_doi = normalize_doi((grobid or {}).get("doi"))
        if grobid_doi and is_plausible_doi(grobid_doi):
            candidates.append(DoiCandidate(grobid_doi, "grobid_doi", "grobid", "medium", "GROBID header metadata"))
        merged, title_source, authors_source, contributors = merge_grobid(pdf_metadata, grobid)
        record.update(merged)
        record["title_source"] = title_source
        record["authors_source"] = authors_source
        record["grobid_doi"] = grobid_doi if is_plausible_doi(grobid_doi) else ""
        record["grobid_title"] = clean((grobid or {}).get("title"))
        record["grobid_authors"] = clean((grobid or {}).get("authors"))
        if grobid_error:
            append_error(record, grobid_error)

        needs_ocr_metadata = (
            args.fallback_allow_ocr
            and args.ocr_metadata_on_grobid_fail
            and (grobid_error or is_bad_title(record.get("title")) or not parse_authors(record.get("authors")))
        )
        if needs_ocr_metadata:
            result = ensure_ocr()
            if result.title and is_bad_title(record.get("title")):
                record["title"] = result.title
                record["title_source"] = "ocr"
                contributors.append("ocr")
            if result.authors and not parse_authors(record.get("authors")):
                record["authors"] = result.authors
                record["authors_source"] = "ocr"
                contributors.append("ocr")
            add_ocr_candidates()
        candidates.extend(maybe_markdown_hits(pdf_path, args.markdown_dir))

        doi, confidence, decision = choose_candidate(candidates, filename_policy=args.filename_doi_policy)
        if not doi and validator and record.get("title"):
            for candidate_doi in validator.title_search(str(record.get("title"))):
                candidates.append(DoiCandidate(candidate_doi, "crossref_title_search", "title_lookup", "review", "title search"))
            doi, confidence, decision = choose_candidate(candidates, filename_policy=args.filename_doi_policy)

        validation = {"verified": False}
        if validator and doi and (confidence in {"review", "low", "medium"} or args.validate_all):
            validation = validator.verify(doi, str(record.get("title") or ""), parse_authors(record.get("authors")))
            if validation.get("verified"):
                doi = str(validation["doi"])
                confidence = "high"
                decision = "title_author_verified"

        if (not doi or confidence == "review") and args.fallback_allow_ocr:
            add_ocr_candidates()
            doi, confidence, decision = choose_candidate(candidates, filename_policy=args.filename_doi_policy)
            if validator and doi and (confidence in {"review", "low", "medium"} or args.validate_all):
                validation = validator.verify(doi, str(record.get("title") or ""), parse_authors(record.get("authors")))
                if validation.get("verified"):
                    doi = str(validation["doi"])
                    confidence = "high"
                    decision = "ocr_candidate_title_author_verified"

        if (not doi or confidence == "review") and args.llm:
            candidates.extend(llm_candidates(record, snippets, args))
            doi, confidence, decision = choose_candidate(candidates, filename_policy=args.filename_doi_policy)
            if validator and doi:
                validation = validator.verify(doi, str(record.get("title") or ""), parse_authors(record.get("authors")))
                if validation.get("verified"):
                    doi = str(validation["doi"])
                    confidence = "high"
                    decision = "llm_candidate_title_author_verified"

        source = ""
        for candidate in candidates:
            if doi and candidate.doi.lower() == doi.lower():
                source = candidate.source
                break
        record.update(
            {
                "status": "ok" if doi and confidence != "review" else "review" if doi else "no_doi",
                "method": decision,
                "doi": doi or "",
                "confidence": confidence,
                "doi_decision": decision,
                "doi_source": source,
                "metadata_source": "+".join(["pdf", *dict.fromkeys(contributors)]),
                "doi_validation_source": "crossref" if validation.get("verified") else "",
                "doi_validation_score": json.dumps({k: validation.get(k) for k in ("title_score", "author_score") if k in validation}, separators=(",", ":")),
                "doi_candidates": [candidate.to_dict() for candidate in candidates if is_plausible_doi(candidate.doi)],
            }
        )
    except Exception as exc:
        record.update({"status": "error", "method": "none", "doi": "", "confidence": "none", "error": f"{type(exc).__name__}: {exc}"})
    return record


def find_pdfs(input_dir: Path, recursive: bool) -> list[Path]:
    iterator = input_dir.rglob("*") if recursive else input_dir.glob("*")
    return sorted(path for path in iterator if path.is_file() and path.suffix.lower() == ".pdf")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone DOI extraction pipeline")
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--grobid-metadata", type=Path)
    parser.add_argument("--grobid-endpoint", default="", help="Optional GROBID processHeaderDocument endpoint")
    parser.add_argument("--grobid-on-missing", action="store_true", help="Call GROBID when title/authors are missing before DOI validation")
    parser.add_argument("--grobid-timeout", type=float, default=180.0)
    parser.add_argument("--markdown-dir", type=Path)
    parser.add_argument("--filename-doi-policy", choices=("trusted", "candidate"), default="candidate")
    parser.add_argument("--validate", action="store_true", help="Validate low/review candidates with Crossref")
    parser.add_argument("--validate-all", action="store_true")
    parser.add_argument(
        "--crossref-mailto",
        default=os.environ.get("CROSSREF_MAILTO", ""),
        help="Contact email added to the Crossref User-Agent to use the polite pool",
    )
    parser.add_argument("--fallback-allow-ocr", action="store_true")
    parser.add_argument(
        "--ocr-metadata-on-grobid-fail",
        "--ocr-metadata-on-missing",
        dest="ocr_metadata_on_grobid_fail",
        action="store_true",
        help="Use OCR to fill title/authors when GROBID fails or metadata remains missing",
    )
    parser.add_argument("--ocr-pages", type=int, default=3)
    parser.add_argument("--ocr-language", default="eng+chi_sim", help="Tesseract language expression, for example eng or eng+chi_sim")
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--llm-base-url", default=os.environ.get("OPENAI_BASE_URL", ""))
    parser.add_argument("--llm-model", default=os.environ.get("OPENAI_MODEL", ""))
    parser.add_argument("--llm-api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    parser.add_argument("--llm-timeout", type=float, default=120.0)
    parser.add_argument(
        "--no-review-postprocess",
        dest="review_postprocess",
        action="store_false",
        help="Disable automatic stage2/stage3/stage4 review-row postprocessing",
    )
    parser.add_argument(
        "--no-review-authority",
        dest="review_authority",
        action="store_false",
        help="Disable DOI.org authority lookup during review-row postprocessing",
    )
    parser.add_argument("--authority-workers", type=int, default=8, help="DOI.org authority lookup workers for review postprocessing")
    parser.add_argument("--authority-timeout", type=float, default=20.0, help="DOI.org authority lookup timeout")
    parser.add_argument(
        "--no-hardcase-postprocess",
        dest="hardcase_postprocess",
        action="store_false",
        help="Disable automatic hard-case DOI recovery after review postprocessing",
    )
    parser.add_argument(
        "--no-hardcase-title-search",
        dest="hardcase_title_search",
        action="store_false",
        help="Disable Crossref/DataCite title search in hard-case postprocessing",
    )
    parser.add_argument("--hardcase-workers", type=int, default=6, help="Workers for hard-case DOI authority lookups")
    parser.add_argument("--hardcase-timeout", type=float, default=15.0, help="Timeout for hard-case DOI authority lookups")
    parser.add_argument("--hardcase-pdf-pages", type=int, default=4, help="PDF front pages to inspect for hard-case title support")
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--fast", action="store_true")
    parser.set_defaults(review_postprocess=True, review_authority=True, hardcase_postprocess=True, hardcase_title_search=True)
    args = parser.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    grobid_by_name = load_grobid_metadata(args.grobid_metadata)
    validator = (
        MetadataValidator(args.output_dir / "metadata_validation_cache.jsonl", mailto=args.crossref_mailto)
        if args.validate or args.validate_all
        else None
    )
    pdfs = find_pdfs(args.input_dir.expanduser(), args.recursive)

    records: list[dict[str, Any]] = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(process_pdf, pdf, args, grobid_by_name, validator) for pdf in pdfs]
        for index, future in enumerate(as_completed(futures), start=1):
            record = future.result()
            records.append(record)
            print(f"{index}/{len(pdfs)}\t{record.get('status')}\t{record.get('doi')}\t{record.get('filename')}", flush=True)

    records.sort(key=lambda row: row.get("path", ""))
    postprocess_summary: dict[str, Any] = {"enabled": False}
    if args.review_postprocess:
        postprocess_summary = postprocess_review_records(
            records,
            args.output_dir,
            authority_workers=max(1, args.authority_workers),
            authority_timeout=args.authority_timeout,
            use_authority=args.review_authority,
        )
        (args.output_dir / "review_postprocess_summary.json").write_text(
            json.dumps(postprocess_summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    hardcase_summary: dict[str, Any] = {"enabled": False}
    if args.hardcase_postprocess:
        hardcase_summary = postprocess_hardcase_records(
            records,
            args.output_dir,
            workers=max(1, args.hardcase_workers),
            timeout=args.hardcase_timeout,
            pdf_pages=max(1, args.hardcase_pdf_pages),
            title_search=args.hardcase_title_search,
        )
    jsonl_path = args.output_dir / "metadata.jsonl"
    tsv_path = args.output_dir / "metadata.tsv"
    with jsonl_path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    with tsv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=SUMMARY_FIELDS, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({field: flatten(record.get(field)) for field in SUMMARY_FIELDS})
    print(
        json.dumps(
            {
                "rows": len(records),
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "jsonl": str(jsonl_path),
                "tsv": str(tsv_path),
                "review_postprocess": postprocess_summary,
                "hardcase_postprocess": hardcase_summary,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
