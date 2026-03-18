import json
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from config import FETCH_LOOKBACK_DAYS, TARGET_JOURNALS


ARXIV_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "arxiv_cache.json"
DEFAULT_FETCH_LIMIT = 100


def _request_bytes(url: str) -> bytes:
    last_error = None
    for attempt in range(3):
        request = Request(url, headers={"User-Agent": "paper-pipeline/1.0"})
        try:
            with urlopen(request, timeout=60) as response:
                return response.read()
        except (TimeoutError, URLError, HTTPError) as exc:
            last_error = exc
            if attempt == 2:
                raise
            sleep_seconds = 1.5 * (attempt + 1)
            if isinstance(exc, HTTPError) and exc.code == 429:
                sleep_seconds = 8 * (attempt + 1)
            time.sleep(sleep_seconds)
    raise last_error  # pragma: no cover


def _get_json(url: str) -> Dict:
    return json.loads(_request_bytes(url).decode("utf-8"))


def _get_text(url: str) -> str:
    return _request_bytes(url).decode("utf-8")


def _lookback_days() -> int:
    return max(1, int(FETCH_LOOKBACK_DAYS))


def _within_lookback_window(timestamp: datetime) -> bool:
    now = datetime.now(timezone.utc)
    return now - timestamp <= timedelta(days=_lookback_days())


def _safe_text(value: str) -> str:
    return " ".join((value or "").split())


def _normalized_identifier(prefix: str, value: str) -> str:
    clean = _safe_text(value).lower()
    return f"{prefix}:{clean}" if clean else ""


def _target_journals() -> List[str]:
    if isinstance(TARGET_JOURNALS, list):
        return [_safe_text(value).lower() for value in TARGET_JOURNALS if _safe_text(str(value))]
    if isinstance(TARGET_JOURNALS, str) and _safe_text(TARGET_JOURNALS):
        return [_safe_text(TARGET_JOURNALS).lower()]
    return []


def _pubmed_journal_matches(*journal_fields: str) -> bool:
    targets = _target_journals()
    if not targets:
        return True

    normalized_fields = [_safe_text(field).lower() for field in journal_fields if _safe_text(field)]
    for target in targets:
        for field in normalized_fields:
            if target == field or target in field or field in target:
                return True
    return False


def _pubmed_search_term(max_items: int) -> str:
    days = _lookback_days()
    day_label = "day" if days == 1 else "days"
    date_clause = f'"last {days} {day_label}"[PDat]'
    targets = _target_journals()
    if not targets:
        return date_clause

    journal_terms = [f'"{journal}"[jour]' for journal in targets]
    journal_clause = " OR ".join(journal_terms)
    return f"({journal_clause}) AND {date_clause}"


def _load_arxiv_cache(limit: int) -> List[Dict]:
    if not ARXIV_CACHE_PATH.exists():
        return []
    payload = json.loads(ARXIV_CACHE_PATH.read_text(encoding="utf-8"))
    papers = payload.get("papers", [])
    return papers[:limit]


def _save_arxiv_cache(papers: List[Dict]) -> None:
    ARXIV_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "papers": papers,
    }
    ARXIV_CACHE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def get_biorxiv(limit: Optional[int] = None) -> List[Dict]:
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=_lookback_days())
    url = f"https://api.biorxiv.org/details/biorxiv/{start_date.isoformat()}/{end_date.isoformat()}"
    payload = _get_json(url)

    papers = []
    max_items = int(limit or DEFAULT_FETCH_LIMIT)
    for item in payload.get("collection", [])[:max_items]:
        title = _safe_text(item.get("title", ""))
        abstract = _safe_text(item.get("abstract", ""))
        doi = item.get("doi", "")
        version = item.get("version", "1")
        if not title or not abstract or not doi:
            continue

        papers.append(
            {
                "title": title,
                "abstract": abstract,
                "url": f"https://www.biorxiv.org/content/{doi}v{version}",
                "source": "bioRxiv",
                "doi": doi,
                "identifier": _normalized_identifier("doi", doi),
            }
        )
    return papers


def get_arxiv(limit: Optional[int] = None) -> List[Dict]:
    max_items = int(limit or DEFAULT_FETCH_LIMIT)
    query = "cat:q-bio.* OR cat:cs.AI"
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_items * 2,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = f"https://export.arxiv.org/api/query?{urlencode(params)}"
    try:
        xml_text = _get_text(url)
    except HTTPError as exc:
        if exc.code == 429:
            cached = _load_arxiv_cache(max_items)
            if cached:
                return cached
        raise
    root = ET.fromstring(xml_text)
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    arxiv_namespace = {"arxiv": "http://arxiv.org/schemas/atom"}

    papers = []
    for entry in root.findall("atom:entry", namespace):
        published_text = entry.findtext("atom:published", default="", namespaces=namespace)
        updated_text = entry.findtext("atom:updated", default="", namespaces=namespace)
        timestamp_text = updated_text or published_text
        if not timestamp_text:
            continue

        timestamp = datetime.strptime(timestamp_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if not _within_lookback_window(timestamp):
            continue

        title = _safe_text(entry.findtext("atom:title", default="", namespaces=namespace))
        abstract = _safe_text(entry.findtext("atom:summary", default="", namespaces=namespace))
        url = _safe_text(entry.findtext("atom:id", default="", namespaces=namespace))
        doi = _safe_text(entry.findtext("arxiv:doi", default="", namespaces=arxiv_namespace))
        arxiv_id = url.rstrip("/").split("/")[-1]
        pdf_url = ""
        for link in entry.findall("atom:link", namespace):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href", "")
                break

        if not title or not abstract or not url:
            continue

        papers.append(
            {
                "title": title,
                "abstract": abstract,
                "url": url,
                "pdf_url": pdf_url,
                "source": "arXiv",
                "doi": doi,
                "identifier": _normalized_identifier("doi", doi) or _normalized_identifier("arxiv", arxiv_id),
            }
        )
        if len(papers) >= max_items:
            break

    if papers:
        _save_arxiv_cache(papers)
    return papers


def get_pubmed(limit: Optional[int] = None) -> List[Dict]:
    max_items = int(limit or DEFAULT_FETCH_LIMIT)
    search_params = {
        "db": "pubmed",
        "retmode": "json",
        "retmax": max_items,
        "sort": "pub date",
        "term": _pubmed_search_term(max_items),
    }
    search_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?{urlencode(search_params)}"
    search_payload = _get_json(search_url)
    id_list = search_payload.get("esearchresult", {}).get("idlist", [])
    if not id_list:
        return []

    fetch_params = {
        "db": "pubmed",
        "id": ",".join(id_list),
        "retmode": "xml",
    }
    fetch_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?{urlencode(fetch_params)}"
    xml_text = _get_text(fetch_url)
    root = ET.fromstring(xml_text)

    papers = []
    for article in root.findall(".//PubmedArticle"):
        pmid = article.findtext(".//PMID", default="")
        title = _safe_text("".join(article.find(".//ArticleTitle").itertext())) if article.find(".//ArticleTitle") is not None else ""
        journal_title = _safe_text(article.findtext(".//Journal/Title", default=""))
        iso_abbreviation = _safe_text(article.findtext(".//Journal/ISOAbbreviation", default=""))
        medline_ta = _safe_text(article.findtext(".//MedlineJournalInfo/MedlineTA", default=""))
        abstract_parts = []
        for node in article.findall(".//Abstract/AbstractText"):
            label = node.attrib.get("Label")
            text = _safe_text("".join(node.itertext()))
            if not text:
                continue
            abstract_parts.append(f"{label}: {text}" if label else text)
        abstract = " ".join(abstract_parts)

        doi = ""
        pmcid = ""
        for article_id in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
            if article_id.attrib.get("IdType") == "doi":
                doi = _safe_text(article_id.text or "")
            if article_id.attrib.get("IdType") == "pmc":
                pmcid = _safe_text(article_id.text or "")

        if not pmid or not title:
            continue
        if not _pubmed_journal_matches(journal_title, iso_abbreviation, medline_ta):
            continue

        paper = {
            "title": title,
            "abstract": abstract,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "source": "PubMed",
            "doi": doi,
            "identifier": _normalized_identifier("doi", doi) or _normalized_identifier("pmid", pmid),
            "journal": journal_title or medline_ta or iso_abbreviation,
        }
        if pmcid:
            paper["pmcid"] = pmcid
        papers.append(paper)

    return papers
