import re
from pathlib import Path
from typing import Dict, Optional
from urllib.request import Request, urlopen


PDF_DIR = Path(__file__).resolve().parent.parent / "pdfs"


def safe_filename(title: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", title).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:120] or "paper"


def _pdf_url_for_paper(paper: Dict) -> Optional[str]:
    if paper.get("local_pdf_path"):
        return None
    if paper.get("pdf_url"):
        return paper["pdf_url"]

    source = paper.get("source", "").lower()
    url = paper.get("url", "")

    if source == "biorxiv" and url:
        return f"{url}.full.pdf"
    if source == "arxiv":
        if paper.get("pdf_url"):
            return paper["pdf_url"]
        if "/abs/" in url:
            return url.replace("/abs/", "/pdf/") + ".pdf"
    if source == "pubmed" and paper.get("pmcid"):
        return f"https://pmc.ncbi.nlm.nih.gov/articles/{paper['pmcid']}/pdf/"
    return None


def download_pdf(paper: Dict) -> Optional[str]:
    if paper.get("local_pdf_path"):
        return paper["local_pdf_path"]

    pdf_url = _pdf_url_for_paper(paper)
    if not pdf_url:
        return None

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{safe_filename(paper.get('title', 'paper'))}.pdf"
    target = PDF_DIR / filename

    request = Request(pdf_url, headers={"User-Agent": "paper-pipeline/1.0"})
    with urlopen(request, timeout=60) as response:
        target.write_bytes(response.read())

    return str(target)
