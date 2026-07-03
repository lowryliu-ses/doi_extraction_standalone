from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .doi import is_plausible_doi, normalize_doi
from .metadata import author_score, clean, is_bad_title, parse_authors, title_score

BAD_LOCAL_TITLE_RE = re.compile(
    r"(accepted manuscript|to cite this version|self-archived|metadata of the chapter|"
    r"dissertation|master'?s thesis|copyright|downloaded by|view article online|"
    r"abstract of the dissertation|peer review information|editor summary|showcasing research)",
    re.IGNORECASE,
)
MAINSTREAM_FILENAME_PREFIX_RE = re.compile(r"^10\.(1016|1021|1039|1002|1149|1109|1038|1007)/", re.IGNORECASE)
RETRYABLE_ERROR_RE = re.compile(r"(429|timeout|timed out|temporar|too many requests)", re.IGNORECASE)

FILENAME_SOURCES = {"filename", "filename_doi", "filename_pii", "filename_ssrn"}


@dataclass(frozen=True)
class PostprocessDecision:
    decision: str
    stage: str
    accepted: bool
    confidence: str
    reason: str
    title_score: float = 0.0
    author_score: float = 0.0
    authority: dict[str, Any] = field(default_factory=dict)
    corrected_doi: str = ""
    tried_dois: tuple[str, ...] = ()


def parse_score_json(value: Any) -> dict[str, float]:
    if isinstance(value, dict):
        data = value
    elif isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
        except Exception:
            return {}
    else:
        return {}
    out: dict[str, float] = {}
    for key in ("title_score", "author_score", "authority_title_score", "authority_author_score"):
        try:
            out[key] = float(data.get(key, 0.0))
        except Exception:
            pass
    return out


def compact_for_filename(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def filename_has_doi(filename: str, doi: str) -> bool:
    return bool(doi) and compact_for_filename(doi) in compact_for_filename(filename)


def candidate_sources(record: dict[str, Any], doi: str) -> list[str]:
    raw = record.get("doi_candidates")
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    if not isinstance(raw, list):
        return []
    sources: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        item_doi = normalize_doi(item.get("doi"))
        if item_doi and item_doi.lower() == doi.lower():
            source = clean(item.get("source"))
            if source:
                sources.append(source)
    return sources


def doi_from_filename(record: dict[str, Any], doi: str) -> bool:
    source = clean(record.get("doi_source"))
    sources = [source, *candidate_sources(record, doi)]
    return any(item in FILENAME_SOURCES or item.startswith("filename") for item in sources) and filename_has_doi(
        clean(record.get("filename")),
        doi,
    )


def local_title_is_bad(value: Any) -> bool:
    text = clean(value)
    return is_bad_title(text) or bool(BAD_LOCAL_TITLE_RE.search(text))


def stage2_decide(record: dict[str, Any]) -> PostprocessDecision:
    doi = normalize_doi(record.get("doi"))
    if not doi or not is_plausible_doi(doi):
        return PostprocessDecision("stage2_unresolved_no_doi", "stage2", False, "none", "no plausible DOI candidate")

    scores = parse_score_json(record.get("doi_validation_score"))
    ts = scores.get("title_score", 0.0)
    aus = scores.get("author_score", 0.0)
    authors = parse_authors(record.get("authors"))
    from_filename = doi_from_filename(record, doi)

    if ts >= 0.985:
        return PostprocessDecision(
            "stage2_accept_high",
            "stage2",
            True,
            "high",
            f"existing validation title score is near-exact ({ts:.3f})",
            ts,
            aus,
        )
    if ts >= 0.94 and (aus >= 0.15 or not authors):
        return PostprocessDecision(
            "stage2_accept_high",
            "stage2",
            True,
            "high",
            f"existing validation title/author scores pass strict thresholds ({ts:.3f}/{aus:.3f})",
            ts,
            aus,
        )
    if ts >= 0.88 and aus >= 0.75:
        return PostprocessDecision(
            "stage2_accept_probable",
            "stage2",
            True,
            "medium",
            f"existing validation has strong author support ({ts:.3f}/{aus:.3f})",
            ts,
            aus,
        )
    if ts >= 0.80 and aus >= 0.90:
        return PostprocessDecision(
            "stage2_accept_probable",
            "stage2",
            True,
            "medium",
            f"existing validation has very strong author support ({ts:.3f}/{aus:.3f})",
            ts,
            aus,
        )
    if from_filename and (ts >= 0.50 or aus >= 0.50):
        return PostprocessDecision(
            "stage2_accept_probable",
            "stage2",
            True,
            "medium",
            f"filename DOI has weak metadata support ({ts:.3f}/{aus:.3f})",
            ts,
            aus,
        )
    return PostprocessDecision(
        "stage2_needs_authority",
        "stage2",
        False,
        "none",
        f"existing evidence below automatic threshold ({ts:.3f}/{aus:.3f})",
        ts,
        aus,
    )


def first_csl_text(value: Any) -> str:
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
    given = clean(author.get("given"))
    family = clean(author.get("family"))
    return " ".join(part for part in (given, family) if part).strip()


def csl_year(data: dict[str, Any]) -> str:
    issued = data.get("issued")
    parts = issued.get("date-parts") if isinstance(issued, dict) else None
    if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
        return clean(parts[0][0])
    return ""


def fetch_doi_csl(doi: str, timeout: float, user_agent: str) -> dict[str, Any]:
    url = "https://doi.org/" + urllib.parse.quote(doi, safe="/")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.citationstyles.csl+json",
            "User-Agent": user_agent,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))

    authors = [csl_author_name(author) for author in data.get("author", []) if isinstance(author, dict)]
    return {
        "doi": doi,
        "resolved": True,
        "authority_doi": normalize_doi(data.get("DOI") or doi),
        "authority_title": first_csl_text(data.get("title")),
        "authority_authors": [author for author in authors if author],
        "authority_container": first_csl_text(data.get("container-title")),
        "authority_publisher": first_csl_text(data.get("publisher")),
        "authority_type": first_csl_text(data.get("type")),
        "authority_year": csl_year(data),
        "error": "",
    }


def unresolved_authority(doi: str, error: str) -> dict[str, Any]:
    return {
        "doi": doi,
        "resolved": False,
        "authority_doi": "",
        "authority_title": "",
        "authority_authors": [],
        "authority_container": "",
        "authority_publisher": "",
        "authority_type": "",
        "authority_year": "",
        "error": error,
    }


def load_authority_cache(path: Path) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return cache
    with path.open(encoding="utf-8", errors="replace") as file:
        for line in file:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict) and item.get("doi"):
                cache[clean(item["doi"]).lower()] = item
    return cache


class DoiAuthorityResolver:
    def __init__(
        self,
        cache_path: Path,
        *,
        timeout: float = 20.0,
        user_agent: str = "doi-standalone-review-postprocess/1.0",
        min_interval: float = 0.03,
    ) -> None:
        self.cache_path = cache_path
        self.timeout = timeout
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.cache = load_authority_cache(cache_path)
        self.lock = threading.Lock()

    def append_cache(self, item: dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")

    def resolve(self, doi: str, *, force: bool = False) -> dict[str, Any]:
        doi = normalize_doi(doi)
        if not doi:
            return unresolved_authority("", "missing DOI")
        key = doi.lower()
        with self.lock:
            cached = self.cache.get(key)
            if cached and (cached.get("resolved") or not force):
                return cached
        try:
            item = fetch_doi_csl(doi, self.timeout, self.user_agent)
        except Exception as exc:
            item = unresolved_authority(doi, f"{type(exc).__name__}: {exc}")
        with self.lock:
            self.cache[key] = item
            self.append_cache(item)
        time.sleep(self.min_interval)
        return item

    def resolve_jobs(self, jobs: Iterable[tuple[str, bool]], max_workers: int) -> dict[str, dict[str, Any]]:
        job_by_key: dict[str, tuple[str, bool]] = {}
        for doi, force in jobs:
            normalized = normalize_doi(doi)
            if not normalized:
                continue
            key = normalized.lower()
            existing = job_by_key.get(key)
            job_by_key[key] = (normalized, force or bool(existing and existing[1]))
        if not job_by_key:
            return {}
        results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            future_by_key = {
                executor.submit(self.resolve, doi, force=force): key
                for key, (doi, force) in job_by_key.items()
            }
            for future in as_completed(future_by_key):
                key = future_by_key[future]
                results[key] = future.result()
        return results


def score_against_authority(record: dict[str, Any], authority: dict[str, Any]) -> tuple[float, float]:
    ts = title_score(record.get("title"), authority.get("authority_title"))
    aus = author_score(parse_authors(record.get("authors")), authority.get("authority_authors") or [])
    return ts, aus


def stage3_decide(record: dict[str, Any], authority: dict[str, Any]) -> PostprocessDecision:
    doi = normalize_doi(record.get("doi"))
    ts, aus = score_against_authority(record, authority)
    local_authors = parse_authors(record.get("authors"))
    from_filename = doi_from_filename(record, doi)

    if not doi:
        return PostprocessDecision("stage3_unresolved_no_doi", "stage3", False, "none", "no DOI candidate", ts, aus, authority)
    if not authority.get("resolved"):
        return PostprocessDecision(
            "stage3_unresolved_not_resolved",
            "stage3",
            False,
            "none",
            f"DOI authority lookup failed: {authority.get('error', '')}",
            ts,
            aus,
            authority,
        )
    if ts >= 0.985:
        return PostprocessDecision(
            "stage3_accept_high",
            "stage3",
            True,
            "high",
            f"DOI authority title is near-exact ({ts:.3f})",
            ts,
            aus,
            authority,
        )
    if ts >= 0.94 and (aus >= 0.15 or not local_authors):
        return PostprocessDecision(
            "stage3_accept_high",
            "stage3",
            True,
            "high",
            f"DOI authority title/author scores pass strict thresholds ({ts:.3f}/{aus:.3f})",
            ts,
            aus,
            authority,
        )
    if ts >= 0.80 and aus >= 0.50:
        return PostprocessDecision(
            "stage3_accept_probable",
            "stage3",
            True,
            "medium",
            f"DOI authority metadata is close to local metadata ({ts:.3f}/{aus:.3f})",
            ts,
            aus,
            authority,
        )
    if from_filename and local_title_is_bad(record.get("title")):
        return PostprocessDecision(
            "stage3_accept_probable",
            "stage3",
            True,
            "medium",
            "filename DOI resolved and local title is missing or repository/header noise",
            ts,
            aus,
            authority,
        )
    if from_filename and (ts >= 0.50 or aus >= 0.50):
        return PostprocessDecision(
            "stage3_accept_probable",
            "stage3",
            True,
            "medium",
            f"filename DOI resolved with weak local support ({ts:.3f}/{aus:.3f})",
            ts,
            aus,
            authority,
        )
    if from_filename and MAINSTREAM_FILENAME_PREFIX_RE.search(doi):
        return PostprocessDecision(
            "stage3_accept_probable",
            "stage3",
            True,
            "medium",
            "filename DOI resolved under a common publisher prefix; local metadata is weak",
            ts,
            aus,
            authority,
        )
    return PostprocessDecision(
        "stage3_conflict_review",
        "stage3",
        False,
        "low",
        f"DOI resolved but local metadata support is insufficient ({ts:.3f}/{aus:.3f})",
        ts,
        aus,
        authority,
    )


def ecs_variants(doi: str) -> list[str]:
    doi = normalize_doi(doi)
    if not doi.lower().startswith("10.1149/") or "/" not in doi:
        return []
    prefix, suffix = doi.split("/", 1)
    if "_" not in suffix:
        return []
    suffix = suffix.replace("_", "/")
    suffix = re.sub(r"^ma(?=\d{4}-\d{2}/)", "MA", suffix, flags=re.IGNORECASE)
    variant = normalize_doi(f"{prefix}/{suffix}")
    return [variant] if variant and variant.lower() != doi.lower() and is_plausible_doi(variant) else []


def stage4_retry_jobs(record: dict[str, Any], previous: PostprocessDecision) -> list[tuple[str, bool]]:
    doi = normalize_doi(record.get("doi"))
    if not doi:
        return []
    jobs: list[tuple[str, bool]] = []
    authority_error = clean(previous.authority.get("error"))
    if RETRYABLE_ERROR_RE.search(authority_error):
        jobs.append((doi, True))
    jobs.extend((variant, False) for variant in ecs_variants(doi))
    return jobs


def stage4_decide(
    record: dict[str, Any],
    authority: dict[str, Any],
    corrected_doi: str,
    tried_dois: Iterable[str],
) -> PostprocessDecision:
    ts, aus = score_against_authority(record, authority)
    local_authors = parse_authors(record.get("authors"))
    from_filename = doi_from_filename(record, normalize_doi(record.get("doi")))
    tried = tuple(dict.fromkeys(doi for doi in tried_dois if doi))

    if not authority.get("resolved"):
        return PostprocessDecision(
            "stage4_unresolved",
            "stage4",
            False,
            "none",
            clean(authority.get("error")) or "no retry/normalized DOI resolved",
            ts,
            aus,
            authority,
            corrected_doi,
            tried,
        )
    if ts >= 0.94 and (aus >= 0.15 or not local_authors):
        return PostprocessDecision(
            "stage4_accept_high",
            "stage4",
            True,
            "high",
            f"corrected/retried DOI authority metadata passes strict thresholds ({ts:.3f}/{aus:.3f})",
            ts,
            aus,
            authority,
            corrected_doi,
            tried,
        )
    if from_filename:
        return PostprocessDecision(
            "stage4_accept_probable",
            "stage4",
            True,
            "medium",
            "filename DOI resolved after correction/retry",
            ts,
            aus,
            authority,
            corrected_doi,
            tried,
        )
    if ts >= 0.80 and aus >= 0.50:
        return PostprocessDecision(
            "stage4_accept_probable",
            "stage4",
            True,
            "medium",
            f"corrected/retried DOI metadata is close to local metadata ({ts:.3f}/{aus:.3f})",
            ts,
            aus,
            authority,
            corrected_doi,
            tried,
        )
    return PostprocessDecision(
        "stage4_conflict_review",
        "stage4",
        False,
        "low",
        f"corrected/retried DOI still conflicts with local metadata ({ts:.3f}/{aus:.3f})",
        ts,
        aus,
        authority,
        corrected_doi,
        tried,
    )


def ensure_original_fields(record: dict[str, Any]) -> None:
    if record.get("original_status"):
        return
    record["original_status"] = record.get("status", "")
    record["original_method"] = record.get("method", "")
    record["original_doi"] = record.get("doi", "")
    record["original_confidence"] = record.get("confidence", "")


def merge_validation_score(record: dict[str, Any], decision: PostprocessDecision) -> None:
    score: dict[str, Any] = {}
    if clean(record.get("doi_validation_score")):
        try:
            score = json.loads(clean(record.get("doi_validation_score")))
            if not isinstance(score, dict):
                score = {}
        except Exception:
            score = {}
    score.update(
        {
            "postprocess_decision": decision.decision,
            "authority_title_score": round(decision.title_score, 4),
            "authority_author_score": round(decision.author_score, 4),
        }
    )
    record["doi_validation_score"] = json.dumps(score, ensure_ascii=False, separators=(",", ":"))


def write_authority_fields(record: dict[str, Any], decision: PostprocessDecision) -> None:
    authority = decision.authority or {}
    record["review_stage"] = decision.stage
    record["postprocess_decision"] = decision.decision
    record["postprocess_confidence"] = decision.confidence
    record["postprocess_reason"] = decision.reason
    record["authority_doi"] = authority.get("authority_doi", "")
    record["authority_title"] = authority.get("authority_title", "")
    record["authority_authors"] = authority.get("authority_authors", [])
    record["authority_container"] = authority.get("authority_container", "")
    record["authority_publisher"] = authority.get("authority_publisher", "")
    record["authority_type"] = authority.get("authority_type", "")
    record["authority_year"] = authority.get("authority_year", "")
    record["authority_title_score"] = f"{decision.title_score:.4f}"
    record["authority_author_score"] = f"{decision.author_score:.4f}"
    record["authority_error"] = authority.get("error", "")
    record["corrected_doi"] = decision.corrected_doi
    record["tried_dois"] = list(decision.tried_dois)


def apply_decision(record: dict[str, Any], decision: PostprocessDecision) -> None:
    ensure_original_fields(record)
    write_authority_fields(record, decision)
    merge_validation_score(record, decision)
    if not decision.accepted:
        return
    accepted_doi = normalize_doi(decision.corrected_doi or decision.authority.get("authority_doi") or record.get("doi"))
    if not accepted_doi or not is_plausible_doi(accepted_doi):
        accepted_doi = normalize_doi(record.get("doi"))
    record["status"] = "ok"
    record["method"] = f"postprocess_{decision.decision}"
    record["doi"] = accepted_doi
    record["confidence"] = decision.confidence
    record["doi_decision"] = decision.decision
    if decision.stage in {"stage3", "stage4"}:
        record["doi_validation_source"] = "doi_authority"
    elif not record.get("doi_validation_source"):
        record["doi_validation_source"] = "postprocess_existing_evidence"


def postprocess_target(record: dict[str, Any]) -> bool:
    if record.get("status") == "ok":
        return False
    doi = normalize_doi(record.get("doi"))
    return bool(doi and is_plausible_doi(doi))


def postprocess_review_records(
    records: list[dict[str, Any]],
    output_dir: Path,
    *,
    authority_workers: int = 8,
    authority_timeout: float = 20.0,
    use_authority: bool = True,
) -> dict[str, Any]:
    target_records = [record for record in records if postprocess_target(record)]
    decisions: list[PostprocessDecision] = []
    stage3_records: list[dict[str, Any]] = []

    for record in target_records:
        ensure_original_fields(record)
        decision = stage2_decide(record)
        decisions.append(decision)
        if decision.accepted:
            apply_decision(record, decision)
        else:
            write_authority_fields(record, decision)
            stage3_records.append(record)

    resolver: DoiAuthorityResolver | None = None
    stage4_records: list[tuple[dict[str, Any], PostprocessDecision]] = []
    if use_authority and stage3_records:
        resolver = DoiAuthorityResolver(output_dir / "doi_authority_cache.jsonl", timeout=authority_timeout)
        stage3_results = resolver.resolve_jobs(((record.get("doi", ""), False) for record in stage3_records), authority_workers)
        for record in stage3_records:
            authority = stage3_results.get(normalize_doi(record.get("doi")).lower()) or unresolved_authority(
                normalize_doi(record.get("doi")),
                "missing authority result",
            )
            decision = stage3_decide(record, authority)
            decisions.append(decision)
            apply_decision(record, decision)
            if not decision.accepted and decision.decision in {"stage3_unresolved_not_resolved", "stage3_conflict_review"}:
                stage4_records.append((record, decision))

    if use_authority and resolver and stage4_records:
        jobs: list[tuple[str, bool]] = []
        jobs_by_record: dict[int, list[str]] = {}
        for record, previous in stage4_records:
            record_jobs = stage4_retry_jobs(record, previous)
            jobs.extend(record_jobs)
            jobs_by_record[id(record)] = [doi for doi, _force in record_jobs]
        stage4_results = resolver.resolve_jobs(jobs, authority_workers)
        for record, _previous in stage4_records:
            tried = jobs_by_record.get(id(record), [])
            best_authority: dict[str, Any] | None = None
            corrected_doi = ""
            for doi in tried:
                authority = stage4_results.get(normalize_doi(doi).lower())
                if authority and authority.get("resolved"):
                    best_authority = authority
                    corrected_doi = normalize_doi(authority.get("authority_doi") or doi)
                    break
            if best_authority is None:
                best_authority = unresolved_authority(normalize_doi(record.get("doi")), "no retry/normalized DOI resolved")
            decision = stage4_decide(record, best_authority, corrected_doi, tried)
            decisions.append(decision)
            apply_decision(record, decision)

    accepted_records = [
        record
        for record in target_records
        if record.get("status") == "ok" and record.get("original_status") and record.get("original_status") != "ok"
    ]
    return {
        "enabled": True,
        "target_rows": len(target_records),
        "accepted_rows": len(accepted_records),
        "remaining_target_rows": len([record for record in target_records if record.get("status") != "ok"]),
        "decision": dict(Counter(decision.decision for decision in decisions)),
        "stage": dict(Counter(decision.stage for decision in decisions)),
    }
