from __future__ import annotations

import urllib.request
from pathlib import Path
from xml.etree import ElementTree as ET

from .metadata import clean

NS = {"tei": "http://www.tei-c.org/ns/1.0"}
BOUNDARY = "----doi-standalone-grobid-boundary"


def node_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return clean("".join(node.itertext()))


def first_text(root: ET.Element, paths: list[str]) -> str:
    for path in paths:
        value = node_text(root.find(path, NS))
        if value:
            return value
    return ""


def first_date(root: ET.Element) -> str:
    for node in root.findall(".//tei:publicationStmt/tei:date", NS):
        value = clean(node.get("when") or node_text(node))
        if value:
            return value
    for node in root.findall(".//tei:sourceDesc//tei:date", NS):
        value = clean(node.get("when") or node_text(node))
        if value:
            return value
    return ""


def extract_authors(root: ET.Element) -> list[str]:
    authors: list[str] = []
    for author in root.findall(".//tei:sourceDesc//tei:analytic/tei:author", NS):
        name = node_text(author.find("tei:persName", NS))
        if name:
            authors.append(name)
    return authors


def extract_doi(root: ET.Element) -> str:
    for idno in root.findall(".//tei:idno", NS):
        if (idno.get("type") or "").lower() == "doi":
            doi = node_text(idno)
            if doi:
                return doi
    return ""


def metadata_from_tei(xml_bytes: bytes) -> dict[str, str]:
    root = ET.fromstring(xml_bytes)
    published_date = first_date(root)
    return {
        "source_pdf": "",
        "tei_xml": "",
        "title": first_text(
            root,
            [
                ".//tei:titleStmt/tei:title",
                ".//tei:sourceDesc//tei:analytic/tei:title",
                ".//tei:sourceDesc//tei:monogr/tei:title",
            ],
        ),
        "doi": extract_doi(root),
        "published_date": published_date,
        "year": published_date[:4] if len(published_date) >= 4 and published_date[:4].isdigit() else "",
        "journal": first_text(root, [".//tei:sourceDesc//tei:monogr/tei:title"]),
        "publisher": first_text(root, [".//tei:publicationStmt/tei:publisher"]),
        "authors": "; ".join(extract_authors(root)),
        "abstract": first_text(root, [".//tei:profileDesc/tei:abstract"]),
    }


def process_header_document(pdf_path: Path, endpoint: str, timeout: float = 180.0) -> dict[str, str]:
    filename = pdf_path.name.replace('"', "_")
    head = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="input"; filename="{filename}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{BOUNDARY}--\r\n".encode("utf-8")
    body = head + pdf_path.read_bytes() + tail
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
            "Accept": "application/xml",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        metadata = metadata_from_tei(response.read())
    metadata["source_pdf"] = pdf_path.name
    return metadata
