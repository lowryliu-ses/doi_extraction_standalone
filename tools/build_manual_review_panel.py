#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any

from doi_pipeline.metadata import clean

csv.field_size_limit(sys.maxsize)


def row_pdf_path(row: dict[str, Any], local_base: Path | None) -> Path:
    if local_base:
        candidate = local_base / "files" / clean(row.get("folder")) / clean(row.get("filename"))
        if candidate.exists():
            return candidate
    return Path(clean(row.get("round2_pdf_path")) or clean(row.get("still_target_path")) or clean(row.get("target_path")) or clean(row.get("path")))


def key(row: dict[str, Any]) -> tuple[str, str]:
    return clean(row.get("folder")), clean(row.get("filename"))


def load_tsv(path: Path | None) -> dict[tuple[str, str], dict[str, str]]:
    if not path or not path.exists():
        return {}
    rows = csv.DictReader(path.open(encoding="utf-8", errors="replace"), delimiter="\t")
    return {key(row): dict(row) for row in rows}


def existing_text_chars(row: dict[str, Any]) -> int:
    value = 0
    for field in ("round2_pdf_text_chars", "hardcase_pdf_text_chars"):
        try:
            value = max(value, int(float(row.get(field) or 0)))
        except Exception:
            pass
    return value


def has_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def suggested_action(row: dict[str, Any], markdown: dict[str, str], ocr: dict[str, str]) -> str:
    text = " ".join([row.get("filename", ""), row.get("title", ""), row.get("round2_title", "")]).lower()
    visible = " ".join([row.get("filename", ""), row.get("title", ""), row.get("round2_title", "")])
    if markdown.get("llm_md_candidate_doi") or ocr.get("ocr_candidate_doi"):
        return "candidate-visible-review"
    if existing_text_chars(row) < 500:
        return "vision-ocr-needed"
    if re.search(r"thesis|dissertation|mémoire|master|doctoral|undergraduate honor|学位|博士|硕士", text):
        return "thesis-repository-search"
    if re.search(r"10\.1201|978|chapter|book|b\d{4}|liquid electrolytes|battery technologies", text):
        return "book-chapter-search"
    if has_cjk(visible):
        return "cnki-wanfang-search"
    if row.get("final_decision") == "no_doi_unresolved":
        return "likely-no-doi"
    return "manual-web-search"


def render_thumbnail(pdf_path: Path, thumb_path: Path, dpi: int) -> str:
    if thumb_path.exists():
        return ""
    try:
        import fitz  # type: ignore

        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        with fitz.open(str(pdf_path)) as doc:
            if len(doc) == 0:
                return "empty PDF"
            page = doc[0]
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            pix.save(str(thumb_path))
        return ""
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def short(value: Any, limit: int = 220) -> str:
    text = clean(value)
    return text if len(text) <= limit else text[: limit - 1] + "..."


def esc(value: Any) -> str:
    return html.escape(clean(value))


def search_links(title: str, doi: str) -> str:
    q = urllib.parse.quote(title or doi)
    links = []
    if title:
        links.extend(
            [
                ("Google Scholar", f"https://scholar.google.com/scholar?q={q}"),
                ("Baidu Scholar", f"https://xueshu.baidu.com/s?wd={q}"),
                ("Crossref", f"https://search.crossref.org/?q={q}"),
            ]
        )
    if doi:
        links.append(("DOI", f"https://doi.org/{urllib.parse.quote(doi, safe='/')}"))
    return " ".join(f'<a target="_blank" href="{html.escape(url)}">{label}</a>' for label, url in links)


def build_html(
    rows: list[dict[str, str]],
    markdown_by_key: dict[tuple[str, str], dict[str, str]],
    ocr_by_key: dict[tuple[str, str], dict[str, str]],
    thumb_errors: dict[tuple[str, str], str],
    out_dir: Path,
) -> str:
    counts = Counter(row["review_action"] for row in rows)
    buttons = "".join(
        f'<button type="button" data-filter="{esc(action)}">{esc(action)} ({count})</button>'
        for action, count in counts.most_common()
    )
    cards: list[str] = []
    for index, row in enumerate(rows, start=1):
        row_key = key(row)
        markdown = markdown_by_key.get(row_key, {})
        ocr = ocr_by_key.get(row_key, {})
        rel_thumb = row.get("review_thumb", "")
        pdf_rel = row.get("review_pdf_rel", "")
        title = clean(row.get("title") or row.get("round2_title") or row.get("content_title") or markdown.get("llm_md_title") or ocr.get("ocr_extracted_title"))
        candidate_doi = clean(
            markdown.get("llm_md_candidate_doi")
            or ocr.get("ocr_candidate_doi")
            or row.get("round2_candidate_doi")
            or row.get("hardcase_candidate_doi")
            or row.get("doi")
        )
        authority_title = clean(
            markdown.get("llm_md_authority_title")
            or ocr.get("ocr_authority_title")
            or row.get("round2_authority_title")
            or row.get("authority_title_new")
        )
        thumb_html = (
            f'<img src="{esc(rel_thumb)}" alt="page 1">'
            if rel_thumb and not thumb_errors.get(row_key)
            else f'<div class="thumb-error">{esc(thumb_errors.get(row_key, "no thumbnail"))}</div>'
        )
        cards.append(
            f"""
<article class="card" data-action="{esc(row['review_action'])}">
  <div class="media">{thumb_html}</div>
  <div class="body">
    <div class="topline"><span>#{index}</span><strong>{esc(row.get('folder'))}</strong><span>{esc(row['review_action'])}</span></div>
    <h2>{esc(row.get('filename'))}</h2>
    <p><a href="{esc(pdf_rel)}" target="_blank">Open PDF</a> {search_links(title, candidate_doi)}</p>
    <dl>
      <dt>Local title</dt><dd>{esc(short(title, 260))}</dd>
      <dt>Candidate DOI</dt><dd>{esc(candidate_doi)}</dd>
      <dt>Authority title</dt><dd>{esc(short(authority_title, 260))}</dd>
      <dt>Decision</dt><dd>{esc(row.get('final_decision'))} / {esc(row.get('strict_reason'))}</dd>
      <dt>Round2</dt><dd>{esc(row.get('round2_reason'))}</dd>
      <dt>Markdown</dt><dd>{esc(markdown.get('llm_md_reason'))} {esc(markdown.get('llm_md_candidate_doi'))}</dd>
      <dt>OCR</dt><dd>{esc(ocr.get('ocr_reason'))} {esc(ocr.get('ocr_candidate_doi'))}</dd>
    </dl>
    <div class="decision">
      <label><input type="radio" name="d{index}" value="accept"> accept</label>
      <label><input type="radio" name="d{index}" value="reject"> reject</label>
      <label><input type="radio" name="d{index}" value="no_doi"> no DOI</label>
      <input placeholder="final DOI / note">
    </div>
  </div>
</article>"""
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Remaining DOI Manual Review</title>
<style>
body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #18202a; }}
header {{ position: sticky; top: 0; z-index: 2; background: #ffffff; border-bottom: 1px solid #d9dee7; padding: 14px 18px; }}
h1 {{ font-size: 20px; margin: 0 0 10px; }}
button {{ margin: 0 6px 6px 0; padding: 7px 10px; border: 1px solid #bcc5d3; background: #fff; border-radius: 6px; cursor: pointer; }}
button.active {{ background: #143d66; color: #fff; border-color: #143d66; }}
main {{ padding: 18px; display: grid; gap: 14px; }}
.card {{ display: grid; grid-template-columns: minmax(220px, 30vw) 1fr; gap: 16px; background: #fff; border: 1px solid #dbe1ea; border-radius: 8px; padding: 12px; }}
.media {{ background: #e8edf4; min-height: 260px; display: flex; align-items: flex-start; justify-content: center; overflow: auto; border-radius: 6px; }}
.media img {{ width: 100%; height: auto; display: block; }}
.thumb-error {{ padding: 20px; color: #7b2432; }}
.topline {{ display: flex; gap: 10px; font-size: 13px; color: #526070; margin-bottom: 6px; }}
h2 {{ font-size: 16px; margin: 0 0 8px; line-height: 1.35; }}
p, dd, dt {{ font-size: 13px; }}
dl {{ display: grid; grid-template-columns: 120px 1fr; gap: 6px 10px; margin: 10px 0; }}
dt {{ color: #5c6877; }}
dd {{ margin: 0; overflow-wrap: anywhere; }}
a {{ color: #095ca8; }}
.decision {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; border-top: 1px solid #edf0f4; padding-top: 10px; }}
.decision input[placeholder] {{ flex: 1 1 260px; padding: 7px; border: 1px solid #c9d1dc; border-radius: 6px; }}
@media (max-width: 760px) {{ .card {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<header>
<h1>Remaining DOI Manual Review ({len(rows)})</h1>
<div><button type="button" data-filter="all" class="active">all ({len(rows)})</button>{buttons}</div>
</header>
<main>
{''.join(cards)}
</main>
<script>
for (const button of document.querySelectorAll('button[data-filter]')) {{
  button.addEventListener('click', () => {{
    const filter = button.dataset.filter;
    document.querySelectorAll('button[data-filter]').forEach(b => b.classList.toggle('active', b === button));
    document.querySelectorAll('.card').forEach(card => {{
      card.style.display = filter === 'all' || card.dataset.action === filter ? 'grid' : 'none';
    }});
  }});
}}
</script>
</body>
</html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an HTML manual review panel for unresolved DOI rows")
    parser.add_argument("input_tsv", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--local-base", type=Path)
    parser.add_argument("--markdown-results", type=Path)
    parser.add_argument("--ocr-results", type=Path)
    parser.add_argument("--thumb-dpi", type=int, default=110)
    args = parser.parse_args(argv)

    rows = list(csv.DictReader(args.input_tsv.open(encoding="utf-8", errors="replace"), delimiter="\t"))
    markdown_by_key = load_tsv(args.markdown_results)
    ocr_by_key = load_tsv(args.ocr_results)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    thumbs = args.output_dir / "thumbs"
    thumb_errors: dict[tuple[str, str], str] = {}

    review_rows: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        row = dict(row)
        row_key = key(row)
        markdown = markdown_by_key.get(row_key, {})
        ocr = ocr_by_key.get(row_key, {})
        pdf_path = row_pdf_path(row, args.local_base)
        thumb_name = f"{index:03d}_{re.sub(r'[^A-Za-z0-9_.-]+', '_', row.get('folder', ''))}_{re.sub(r'[^A-Za-z0-9_.-]+', '_', Path(row.get('filename', 'file')).stem)[:80]}.png"
        thumb_path = thumbs / thumb_name
        error = render_thumbnail(pdf_path, thumb_path, args.thumb_dpi)
        if error:
            thumb_errors[row_key] = error
        row["review_action"] = suggested_action(row, markdown, ocr)
        row["review_pdf_path"] = str(pdf_path)
        row["review_pdf_rel"] = Path(row["review_pdf_path"]).relative_to(args.output_dir).as_posix() if False else str(Path("../../") / pdf_path)
        row["review_thumb"] = str(Path("thumbs") / thumb_name)
        review_rows.append(row)

    review_fields = [
        "review_action",
        "folder",
        "filename",
        "review_pdf_path",
        "final_decision",
        "strict_reason",
        "doi",
        "round2_candidate_doi",
        "round2_authority_title",
        "title",
        "authors",
        "round2_reason",
    ]
    with (args.output_dir / "review_queue.tsv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=review_fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(review_rows)

    template_fields = ["folder", "filename", "decision", "final_doi", "note"]
    with (args.output_dir / "review_decisions_template.tsv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=template_fields, delimiter="\t")
        writer.writeheader()
        for row in review_rows:
            writer.writerow({"folder": row.get("folder"), "filename": row.get("filename"), "decision": "", "final_doi": "", "note": ""})

    html_text = build_html(review_rows, markdown_by_key, ocr_by_key, thumb_errors, args.output_dir)
    (args.output_dir / "index.html").write_text(html_text, encoding="utf-8")
    summary = {
        "rows": len(review_rows),
        "by_action": dict(Counter(row["review_action"] for row in review_rows)),
        "thumb_errors": {f"{folder}/{filename}": error for (folder, filename), error in thumb_errors.items()},
    }
    (args.output_dir / "manual_review_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
