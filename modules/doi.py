import html
import json
import re
from typing import Dict, List, Tuple
from urllib.parse import quote
from urllib.request import Request, urlopen


def normalize_doi_text(raw_text: str) -> List[str]:
    tokens = re.split(r"[\s,;]+", raw_text.strip())
    dois = []
    seen = set()
    for token in tokens:
        clean = token.strip()
        if not clean:
            continue
        clean = clean.replace("https://doi.org/", "").replace("http://doi.org/", "")
        clean = clean.replace("doi.org/", "").replace("DOI:", "").replace("doi:", "").strip()
        key = clean.lower()
        if key and key not in seen:
            seen.add(key)
            dois.append(clean)
    return dois


def _strip_jats_tags(text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", text or "")
    return " ".join(html.unescape(no_tags).split())


def _request_json(url: str, headers: Dict[str, str]) -> Dict:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_paper_by_doi(doi: str) -> Dict:
    encoded = quote(doi, safe="")
    url = f"https://api.crossref.org/works/{encoded}"
    payload = _request_json(url, headers={"User-Agent": "paper-pipeline/1.0", "Accept": "application/json"})
    message = payload.get("message", {})

    title_list = message.get("title") or []
    title = " ".join(str(title_list[0]).split()) if title_list else doi
    abstract = _strip_jats_tags(message.get("abstract", ""))
    landing_url = (message.get("URL") or f"https://doi.org/{doi}").strip()
    journal_list = message.get("container-title") or []
    journal = " ".join(str(journal_list[0]).split()) if journal_list else ""

    pdf_url = ""
    for link in message.get("link", []) or []:
        content_type = (link.get("content-type") or "").lower()
        if content_type == "application/pdf":
            pdf_url = (link.get("URL") or "").strip()
            break

    return {
        "title": title,
        "abstract": abstract,
        "url": landing_url,
        "source": "DOI",
        "doi": doi,
        "identifier": f"doi:{doi.lower()}",
        "journal": journal,
        "pdf_url": pdf_url,
    }


def fetch_papers_by_dois(dois: List[str]) -> Tuple[List[Dict], List[Tuple[str, str]]]:
    papers = []
    errors = []
    for doi in dois:
        try:
            papers.append(fetch_paper_by_doi(doi))
        except Exception as exc:
            errors.append((doi, exc.__class__.__name__))
    return papers, errors
