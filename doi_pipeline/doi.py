from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

DOI_SUFFIX_CHARS = r"-._;()/:+A-Z0-9_"
DOI_RE = re.compile(
    rf"(?:(?:doi\s*[:=]\s*)|(?:https?://(?:dx\.)?doi\.org/)|(?:(?:dx\.)?doi\.org/))?"
    rf"(10\.\d{{4,9}}/[{DOI_SUFFIX_CHARS}]+)",
    re.IGNORECASE,
)
SPACED_DOI_RE = re.compile(
    r"10\s*\.\s*\d{4,9}\s*/\s*[A-Za-z0-9]+(?:\s*[._;()/:+\-_]\s*[A-Za-z0-9]+)*",
    re.IGNORECASE,
)
LEADING_JUNK = " \t\r\n\"'<>[]{}("
TRAILING_JUNK = " \t\r\n\"'<>[]{}.,;:)"
PLACEHOLDER_DOIS = {"10.1039/x0xx00000x"}
IOP_PLACEHOLDER_DOI_RE = re.compile(r"^10\.1088/2053-1591/0/0/0+$", re.IGNORECASE)
PLACEHOLDER_DOI_TEXT_RE = re.compile(
    r"(?:please|insert|manuscript|placeholder|yourdoi|x0xx|xx00000)",
    re.IGNORECASE,
)
REPOSITORY_FILENAME_DOI_PREFIXES = (
    "10.48550/",
    "10.5281/",
    "10.26434/",
    "10.3929/",
    "10.24406/",
    "10.6084/",
    "10.17605/",
)


@dataclass(frozen=True)
class DoiHit:
    doi: str
    source: str
    context: str = ""


@dataclass(frozen=True)
class DoiCandidate:
    doi: str
    source: str
    source_group: str
    raw_confidence: str
    context: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def decode_bytes(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # latin-1 maps every byte, so no DOI characters are silently dropped.
        return data.decode("latin-1")


def strip_doi_trailing(doi: str) -> str:
    while doi:
        last = doi[-1]
        if last in ".,;:'\"]}>":
            doi = doi[:-1]
            continue
        if last == ")" and doi.count(")") > doi.count("("):
            doi = doi[:-1]
            continue
        break
    return doi


def normalize_research_square_version(doi: str) -> str:
    return re.sub(
        r"^(10\.21203/rs\.3\.rs-[A-Za-z0-9-]+)_v(\d+)$",
        r"\1/v\2",
        doi,
        flags=re.IGNORECASE,
    )


def normalize_wiley_language_version(doi: str) -> str:
    return re.sub(r"^(10\.1002/)ange(\.\d+)$", r"\1anie\2", doi, flags=re.IGNORECASE)


def strip_wiley_text_tail(doi: str) -> str:
    match = re.fullmatch(r"(10\.1002/[A-Za-z]{2,12}\.\d{7,9}[A-Za-z]?)[.:()].+", doi, re.IGNORECASE)
    if match and is_plausible_doi(match.group(1)):
        return match.group(1)
    match = re.fullmatch(r"(10\.1002/[A-Za-z]{2,12}\.\d{7,9})[a-z]{4,}.+", doi, re.IGNORECASE)
    if match and is_plausible_doi(match.group(1)):
        return match.group(1)
    return doi


def strip_sentence_title_tail(doi: str) -> str:
    while True:
        match = re.fullmatch(r"(.+\d)\.([A-Z][A-Za-z]{3,})", doi)
        if not match:
            return doi
        candidate = match.group(1)
        if not is_plausible_doi(candidate):
            return doi
        doi = candidate


def normalize_doi(value: object) -> str:
    doi = str(value or "").strip(LEADING_JUNK + TRAILING_JUNK)
    doi = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"(?i)^(?:dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"(?i)^doi\s*[:=]\s*", "", doi)
    doi = doi.replace("\\/", "/")
    doi = re.sub(r"\\\s*", "", doi)
    doi = re.sub(r"\s+", "", doi)
    doi = re.split(r"[;,，。]+(?=10\.\d{4,9}/)", doi, maxsplit=1, flags=re.IGNORECASE)[0]
    doi = doi.replace("&gt", "").replace("&lt", "")
    doi = re.sub(r"\)(?:Tj|TJ)$", "", doi, flags=re.IGNORECASE)
    doi = re.split(r"\)/[A-Za-z]+", doi, maxsplit=1)[0]
    doi = re.split(r"/suppl_file/", doi, maxsplit=1, flags=re.IGNORECASE)[0]
    doi = re.split(r"\.?doi[:=]", doi, maxsplit=1, flags=re.IGNORECASE)[0]
    doi = re.sub(r"^(10\.1002/\d{10,13})\.fmatter$", r"\1", doi, flags=re.IGNORECASE)
    doi = re.split(r"(?:</|<|\s|%0A|%0D)", doi, maxsplit=1, flags=re.IGNORECASE)[0]
    doi = normalize_research_square_version(doi)
    doi = normalize_wiley_language_version(doi)
    doi = strip_wiley_text_tail(doi)
    doi = strip_sentence_title_tail(doi)
    return strip_doi_trailing(doi)


def is_plausible_doi(doi: str) -> bool:
    if not doi.lower().startswith("10.") or "/" not in doi:
        return False
    if len(doi) < 8 or len(doi) > 300:
        return False
    if doi.lower() in PLACEHOLDER_DOIS or IOP_PLACEHOLDER_DOI_RE.fullmatch(doi):
        return False
    suffix = doi.split("/", 1)[1]
    if PLACEHOLDER_DOI_TEXT_RE.search(doi):
        return False
    if suffix.startswith("((") or doi.count("(") != doi.count(")"):
        return False
    if re.search(r"/\(?ISSN\)?", doi, re.IGNORECASE):
        return False
    return bool(re.fullmatch(rf"10\.\d{{4,9}}/[{DOI_SUFFIX_CHARS}]+", doi, re.IGNORECASE))


def context_around(text: str, start: int, end: int, radius: int = 80) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - radius) : min(len(text), end + radius)]).strip()


def find_dois(text: str, source: str, *, allow_spaced: bool = False) -> list[DoiHit]:
    hits: list[DoiHit] = []
    versions = [text]
    no_continuations = re.sub(r"\\\s+", "", text)
    if no_continuations != text:
        versions.append(no_continuations)
    for searchable in versions:
        for match in DOI_RE.finditer(searchable):
            doi = normalize_doi(match.group(1))
            if is_plausible_doi(doi):
                hits.append(DoiHit(doi, source, context_around(searchable, *match.span())))
        if allow_spaced:
            for match in SPACED_DOI_RE.finditer(searchable):
                doi = normalize_doi(match.group(0))
                if is_plausible_doi(doi):
                    hits.append(DoiHit(doi, source, context_around(searchable, *match.span())))
    return hits


def doi_from_elsevier_pii(text: str) -> str | None:
    if re.match(r"10\.1017", text.strip(), re.IGNORECASE):
        return None
    compact = re.sub(r"[^A-Za-z0-9]", "", text)
    match = re.match(r"101016S(\d{8})(\d{2})(\d{5})([0-9X])", compact, re.IGNORECASE)
    if not match:
        match = re.match(r"S(\d{8})(\d{2})(\d{5})([0-9X])", compact, re.IGNORECASE)
    if not match:
        return None
    issn, year, article, check = match.groups()
    doi = f"10.1016/S{issn[:4]}-{issn[4:]}({year}){article}-{check.upper()}"
    return doi if is_plausible_doi(doi) else None


def doi_from_filename_token(token: str) -> str | None:
    token = token.strip(LEADING_JUNK + TRAILING_JUNK)
    token = re.sub(r"(?i)^doi[:=_-]*", "", token)
    token = re.sub(r"(?i)\.pdf$", "", token).rstrip(".").replace("@", "_")
    match = re.match(r"^(10\.\d{4,9})([_-])(.+)$", token, re.IGNORECASE)
    if not match:
        return None
    prefix, _separator, suffix = match.groups()
    suffix = suffix.strip("._-")
    if not suffix or re.search(r"(?:^|[_\-.])(si|supp|supporting)(?:[_\-.]|\d{1,4}$|$)", suffix, re.IGNORECASE):
        return None
    if prefix.lower() == "10.1088" and re.match(r"^\d{4}-\d{4}_[A-Za-z0-9]", suffix):
        suffix = suffix.replace("_", "/", 1)
    if prefix.lower() == "10.4028" and re.match(r"^www\.scientific\.net_[A-Za-z]", suffix, re.IGNORECASE):
        suffix = suffix.replace("_", "/", 1)
    doi = normalize_doi(f"{prefix}/{suffix}")
    return doi if is_plausible_doi(doi) else None


def scan_filename(path: Path) -> list[DoiHit]:
    stem = path.stem
    hits = find_dois(stem, "filename")
    pii = doi_from_elsevier_pii(stem)
    if pii:
        hits.append(DoiHit(pii, "filename_pii", stem))
    ssrn = re.search(r"(?:^|[^A-Za-z0-9])ssrn[-_]?(\d{5,10})(?:[^0-9]|$)", stem, re.IGNORECASE)
    if ssrn:
        hits.append(DoiHit(f"10.2139/ssrn.{ssrn.group(1)}", "filename_ssrn", stem))
    for match in re.finditer(
        r"(?<![0-9A-Za-z])10\.\d{4,9}[_@-][A-Za-z0-9][A-Za-z0-9._;()/:+@-]*(?:_[A-Za-z0-9][A-Za-z0-9._;()/:+-]*)?",
        stem,
        re.IGNORECASE,
    ):
        doi = doi_from_filename_token(match.group(0))
        if doi:
            hits.append(DoiHit(doi, "filename_doi", stem))
    return hits


def unique_hits(hits: Iterable[DoiHit]) -> list[DoiHit]:
    seen: set[tuple[str, str]] = set()
    out: list[DoiHit] = []
    for hit in hits:
        key = (hit.doi.lower(), hit.source)
        if key not in seen:
            seen.add(key)
            out.append(hit)
    return out


def source_group(source: str) -> str:
    if source.startswith("filename"):
        return "filename"
    if source.startswith("grobid"):
        return "grobid"
    if source.startswith("markdown"):
        return "markdown"
    if source.startswith("ocr"):
        return "ocr"
    if source.startswith("llm"):
        return "llm"
    if source.startswith(("crossref", "openalex")):
        return "title_lookup"
    if source.startswith(("xmp_", "pypdf", "pdf_", "raw_pdf", "flate_stream")):
        return "pdf_internal"
    return "unknown"


def raw_confidence_for_source(source: str) -> str:
    group = source_group(source)
    if group == "filename" or source.startswith(("markdown_references", "ocr_references")):
        return "review"
    if source.startswith(("xmp_tag_prism:doi", "xmp_tag_dc:identifier", "markdown_front", "markdown_link")):
        return "high"
    if source.startswith(("xmp_metadata", "pypdf_metadata", "pdf_uri_annotation", "raw_pdf_bytes")):
        return "medium"
    return "low"


def hits_to_candidates(hits: Iterable[DoiHit]) -> list[DoiCandidate]:
    candidates: list[DoiCandidate] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        key = (hit.doi.lower(), hit.source)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            DoiCandidate(
                doi=hit.doi,
                source=hit.source,
                source_group=source_group(hit.source),
                raw_confidence=raw_confidence_for_source(hit.source),
                context=hit.context,
            )
        )
    return candidates


def choose_candidate(candidates: list[DoiCandidate], *, filename_policy: str = "candidate") -> tuple[str | None, str, str]:
    candidates = [candidate for candidate in candidates if is_plausible_doi(candidate.doi)]
    if not candidates:
        return None, "none", "no_candidate"
    grouped: dict[str, list[DoiCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.doi.lower(), []).append(candidate)
    best_key = max(
        grouped,
        key=lambda key: (
            len({item.source_group for item in grouped[key] if item.source_group != "filename"}),
            max({"review": 1, "low": 1, "medium": 2, "high": 3}.get(item.raw_confidence, 0) for item in grouped[key]),
            len(grouped[key]),
        ),
    )
    best = grouped[best_key][0].doi
    groups = {item.source_group for item in grouped[best_key]}
    if groups <= {"filename"} and filename_policy == "candidate":
        return best, "review", "filename_only_candidate_requires_validation"
    if len(groups - {"filename"}) >= 2:
        return best, "high", "multiple_independent_sources"
    confidence = max((item.raw_confidence for item in grouped[best_key]), key=lambda c: {"review": 1, "low": 1, "medium": 2, "high": 3}.get(c, 0))
    return best, confidence, "selected_by_source_weight"


def scan_raw_pdf(data: bytes) -> list[DoiHit]:
    text = decode_bytes(data)
    hits = find_dois(text, "raw_pdf_bytes")
    for match in re.finditer(rb"/URI\s*\((.*?)\)", data, re.IGNORECASE | re.DOTALL):
        hits.extend(find_dois(decode_bytes(match.group(1)), "pdf_uri_annotation"))
    return hits


def scan_xmp_metadata(data: bytes) -> list[DoiHit]:
    text = decode_bytes(data)
    hits: list[DoiHit] = []
    for index, block in enumerate(re.findall(r"<x:xmpmeta\b.*?</x:xmpmeta>", text, flags=re.IGNORECASE | re.DOTALL)):
        hits.extend(find_dois(block, f"xmp_metadata_{index}"))
    for tag in ("prism:doi", "dc:identifier", "prism:url"):
        for index, match in enumerate(re.finditer(rf"<{re.escape(tag)}[^>]*>(.*?)</{re.escape(tag)}>", text, re.IGNORECASE | re.DOTALL)):
            hits.extend(find_dois(match.group(1), f"xmp_tag_{tag}_{index}"))
    return hits


def scan_with_pypdf(path: Path, max_pages: int = 3) -> tuple[list[DoiHit], dict[str, object], str]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return [], {}, ""
    hits: list[DoiHit] = []
    metadata: dict[str, object] = {}
    page_text = ""
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                pass
        info = reader.metadata or {}
        info_text = " ".join(str(value) for value in dict(info).values() if value)
        hits.extend(find_dois(info_text, "pypdf_metadata"))
        metadata["title"] = str(info.get("/Title") or "").strip() or None
        author = str(info.get("/Author") or "").strip()
        if author:
            metadata["authors"] = [author]
        pages = []
        for index, page in enumerate(reader.pages[:max_pages], start=1):
            text = page.extract_text() or ""
            pages.append(text)
            hits.extend(find_dois(text, f"pypdf_page_{index}", allow_spaced=True))
        page_text = "\n".join(pages)
    except Exception:
        return hits, metadata, page_text
    return hits, {key: value for key, value in metadata.items() if value}, page_text


def extract_pdf_hits(path: Path, *, fast: bool = False) -> tuple[list[DoiHit], dict[str, object], str]:
    if fast and path.stat().st_size > 25_000_000:
        with path.open("rb") as file:
            data = file.read(20_000_000)
    else:
        data = path.read_bytes()
    hits: list[DoiHit] = []
    hits.extend(scan_filename(path))
    hits.extend(scan_raw_pdf(data))
    hits.extend(scan_xmp_metadata(data))
    pypdf_hits, metadata, page_text = scan_with_pypdf(path)
    hits.extend(pypdf_hits)
    return unique_hits(hits), metadata, page_text


def split_markdown_sections(text: str, front_lines: int = 150) -> tuple[str, str, str]:
    lines = text.splitlines()
    references_start = len(lines)
    for index, line in enumerate(lines):
        if re.match(r"^#*\s*(references|bibliography)\s*$", line.strip(), re.IGNORECASE):
            references_start = index
            break
    return "\n".join(lines[: min(front_lines, references_start)]), "\n".join(lines[front_lines:references_start]), "\n".join(lines[references_start:])


def extract_markdown_hits(text: str, source_prefix: str = "markdown") -> list[DoiHit]:
    front, body, references = split_markdown_sections(text)
    hits = find_dois(front, f"{source_prefix}_front", allow_spaced=True)
    hits.extend(find_dois(body, f"{source_prefix}_body", allow_spaced=True))
    hits.extend(find_dois(references, f"{source_prefix}_references", allow_spaced=True))
    return unique_hits(hits)
