from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .metadata import clean, is_bad_title


@dataclass
class OcrResult:
    text: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    error: str = ""
    pages_read: int = 0


BAD_FRONT_LINE_RE = re.compile(
    r"^(?:abstract|keywords?|references|bibliography|introduction|article|research article|"
    r"original article|communication|letter|contents|table of contents|received|accepted|"
    r"published|available online|corresponding author|supplementary|supporting information|"
    r"cite this)\b",
    re.IGNORECASE,
)
META_LINE_RE = re.compile(
    r"(?:\bdoi\b|https?://|www\.|copyright|\bissn\b|\bisbn\b|\bvol(?:ume)?\b|"
    r"\bissue\b|\bpages?\b|\bpp\.\b|\bet al\.\b|\borcid\b|@)",
    re.IGNORECASE,
)
AFFILIATION_RE = re.compile(
    r"\b(?:university|college|institute|department|school|laboratory|lab\b|center|centre|"
    r"academy|faculty|hospital|corporation|company|inc\.|ltd\.|gmbh|address|email)\b",
    re.IGNORECASE,
)
AUTHOR_SPLIT_RE = re.compile(r"\s*(?:;|\band\b|&|,)\s*")


def normalize_ocr_line(line: str) -> str:
    text = clean(line.replace("\x0c", " "))
    text = re.sub(r"\s+", " ", text)
    return text.strip(" |")


def front_lines(text: str, limit: int = 140) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = normalize_ocr_line(raw)
        if line:
            lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def mostly_letters(line: str) -> bool:
    compact = re.sub(r"\s+", "", line)
    if not compact:
        return False
    letters = sum(1 for char in compact if char.isalpha())
    return letters / max(len(compact), 1) >= 0.45


def is_meta_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if BAD_FRONT_LINE_RE.search(stripped) or META_LINE_RE.search(stripped):
        return True
    if stripped.lower().endswith(".pdf") or stripped.lower().startswith("microsoft word"):
        return True
    if re.fullmatch(r"[\d\s.,:;/()-]+", stripped):
        return True
    if stripped.startswith("10.") or re.match(r"^\d+\s*/\s*\d+$", stripped):
        return True
    return False


def clean_title_part(line: str) -> str:
    text = normalize_ocr_line(line)
    text = re.sub(r"^(?:\S\s+)?check for updates[.:]?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\d{1,6}\s+", "", text)
    text = re.sub(r"^[—_\-–\s]+(?=[A-Za-z])", "", text)
    if re.match(r"^[\u4e00-\u9fff]\s+[A-Za-z]", text):
        text = text[2:].strip()
    return text


def clean_author_part(value: str) -> str:
    text = clean(value)
    text = re.sub(r"[®©™]", "", text)
    text = re.sub(r"\([^)]*(?:university|institute|department|school|lab|email|@)[^)]*\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=[A-Za-z])(?:\d+|[*#]+)+", "", text)
    text = re.sub(r"\[[^\]]+\]", "", text)
    text = re.sub(r"^[\d\s,.*#-]+|[\d\s,.*#-]+$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def looks_like_name(part: str) -> bool:
    if not part or AFFILIATION_RE.search(part) or "@" in part:
        return False
    words = re.findall(r"[A-Za-z][A-Za-z'.-]*", part)
    if len(words) < 2 or len(words) > 7:
        return False
    capitalized = sum(1 for word in words if word[:1].isupper() or re.fullmatch(r"[A-Z]\.?", word))
    if capitalized < min(2, len(words)):
        return False
    lower = " ".join(words).lower()
    if any(token in lower.split() for token in ("using", "based", "effect", "toward", "towards", "with")):
        return False
    return True


def split_author_line(line: str) -> list[str]:
    if is_meta_line(line) or AFFILIATION_RE.search(line):
        return []
    if len(line) > 220:
        return []
    parts = [clean_author_part(part) for part in AUTHOR_SPLIT_RE.split(line)]
    authors = [part for part in parts if looks_like_name(part)]
    if len(authors) >= 2:
        return authors
    single = clean_author_part(line)
    return [single] if looks_like_name(single) else []


def looks_like_author_line(line: str) -> bool:
    return bool(split_author_line(line))


def title_line_candidate(line: str) -> bool:
    if is_meta_line(line) or AFFILIATION_RE.search(line) or looks_like_author_line(line):
        return False
    if is_bad_title(line) or len(line) > 260 or not mostly_letters(line):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z0-9+-]*", line)
    return len(words) >= 4 or len(line) >= 25


def title_continuation_candidate(line: str) -> bool:
    if is_meta_line(line) or AFFILIATION_RE.search(line) or looks_like_author_line(line):
        return False
    if len(line) > 180 or not mostly_letters(line):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z0-9+-]*", line)
    return len(words) >= 2 or len(line) >= 12


def abstract_index(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if re.match(r"^abstract\b", line, re.IGNORECASE):
            return index
    return len(lines)


def extract_title_authors_from_text(text: str) -> tuple[str, list[str]]:
    lines = front_lines(text)
    search_limit = min(abstract_index(lines), 90)
    title = ""
    title_end = -1
    for index, line in enumerate(lines[:search_limit]):
        if not title_line_candidate(line):
            continue
        block = [clean_title_part(line)]
        cursor = index + 1
        skipped_meta = 0
        while cursor < min(index + 6, search_limit):
            next_line = lines[cursor]
            cleaned_next = clean_title_part(next_line)
            if is_meta_line(next_line) and skipped_meta < 2:
                skipped_meta += 1
                cursor += 1
                continue
            if not title_continuation_candidate(cleaned_next):
                break
            block.append(cleaned_next)
            cursor += 1
        title = clean(" ".join(block))
        title_end = cursor - 1
        break

    authors: list[str] = []
    if title_end >= 0:
        for line in lines[title_end + 1 : min(title_end + 13, search_limit)]:
            parsed = split_author_line(line)
            if parsed:
                authors.extend(parsed)
                continue
            if authors or AFFILIATION_RE.search(line) or BAD_FRONT_LINE_RE.search(line):
                break
    return title, list(dict.fromkeys(authors))


def ocr_pdf_front_cli(pdf_path: Path, pages: int = 3, dpi: int = 300, language: str = "eng+chi_sim") -> OcrResult:
    gs = shutil.which("gs")
    tesseract = shutil.which("tesseract")
    if not gs or not tesseract:
        missing = ", ".join(name for name, path in (("gs", gs), ("tesseract", tesseract)) if not path)
        return OcrResult(error=f"ocr unavailable: missing command(s): {missing}")

    text_parts: list[str] = []
    pages_read = 0
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="doi-ocr-") as tmpdir:
        tmp = Path(tmpdir)
        for page in range(1, max(1, pages) + 1):
            image_path = tmp / f"page-{page}.png"
            render = subprocess.run(
                [
                    gs,
                    "-dSAFER",
                    "-dBATCH",
                    "-dNOPAUSE",
                    "-sDEVICE=png16m",
                    f"-r{dpi}",
                    f"-dFirstPage={page}",
                    f"-dLastPage={page}",
                    f"-sOutputFile={image_path}",
                    str(pdf_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if render.returncode != 0 or not image_path.exists():
                errors.append(clean(render.stderr or render.stdout))
                continue
            ocr = subprocess.run(
                [tesseract, str(image_path), "stdout", "-l", language],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if ocr.returncode != 0:
                errors.append(clean(ocr.stderr or ocr.stdout))
                continue
            text_parts.append(ocr.stdout)
            pages_read += 1

    text = "\n".join(text_parts)
    if not text.strip():
        return OcrResult(error=f"ocr failed: {' | '.join(error for error in errors if error)[:500]}", pages_read=pages_read)
    title, authors = extract_title_authors_from_text(text)
    return OcrResult(text=text, title=title, authors=authors, pages_read=pages_read)


def ocr_pdf_front(pdf_path: Path, pages: int = 3, dpi: int = 250, language: str = "eng+chi_sim") -> OcrResult:
    if pages <= 0:
        return OcrResult()
    try:
        import fitz  # type: ignore
        from PIL import Image  # type: ignore
    except Exception:
        return ocr_pdf_front_cli(pdf_path, pages=pages, dpi=max(dpi, 300), language=language)

    text_parts: list[str] = []
    pages_read = 0
    try:
        try:
            import pytesseract  # type: ignore

            with fitz.open(str(pdf_path)) as doc:
                for index in range(min(pages, len(doc))):
                    page = doc[index]
                    pix = page.get_pixmap(dpi=dpi)
                    mode = "RGB" if pix.alpha == 0 else "RGBA"
                    image = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
                    text_parts.append(pytesseract.image_to_string(image, lang=language))
                    pages_read += 1
        except Exception:
            tesseract = shutil.which("tesseract")
            if not tesseract:
                raise
            with tempfile.TemporaryDirectory(prefix="doi-ocr-fitz-") as tmpdir:
                tmp = Path(tmpdir)
                with fitz.open(str(pdf_path)) as doc:
                    for index in range(min(pages, len(doc))):
                        page = doc[index]
                        pix = page.get_pixmap(dpi=dpi)
                        mode = "RGB" if pix.alpha == 0 else "RGBA"
                        image = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
                        image_path = tmp / f"page-{index + 1}.png"
                        image.save(image_path)
                        ocr = subprocess.run(
                            [tesseract, str(image_path), "stdout", "-l", language],
                            capture_output=True,
                            text=True,
                            timeout=120,
                        )
                        if ocr.returncode == 0:
                            text_parts.append(ocr.stdout)
                            pages_read += 1
    except Exception as exc:
        cli_result = ocr_pdf_front_cli(pdf_path, pages=pages, dpi=max(dpi, 300), language=language)
        if cli_result.text:
            return cli_result
        return OcrResult(error=f"ocr failed: {type(exc).__name__}: {exc}; {cli_result.error}", pages_read=pages_read)

    text = "\n".join(text_parts)
    title, authors = extract_title_authors_from_text(text)
    return OcrResult(text=text, title=title, authors=authors, pages_read=pages_read)
