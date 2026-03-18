import argparse
import json
from pathlib import Path
import shutil
import sys
import re
from typing import Dict, List, Optional, Set, Tuple

from config import (
    RESEARCH_DIRECTION,
    RUN_PUBMED,
    TARGET_JOURNALS,
    TITLE_SCREEN_ARXIV,
    TITLE_SCREEN_BIORXIV,
    TITLE_SCREEN_PUBMED,
    TOP_K,
)
from modules.doi import fetch_papers_by_dois, normalize_doi_text
from modules.fetch import get_arxiv, get_biorxiv, get_pubmed
from modules.pdf import download_pdf, safe_filename
from modules.summarize import screen_titles_batch_result, summarize_deep_result, summarize_shallow_result
from modules.zotero import (
    add_collection_note,
    add_note_to_existing_item,
    add_to_zotero,
    create_run_collection,
    get_collection_local_status,
    list_collection_items,
)


ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "data" / "cache.json"
SEEN_IDS_PATH = ROOT / "data" / "seen_ids.json"
OUTPUT_DIR = ROOT / "output"
DAILY_SUMMARY_PATH = OUTPUT_DIR / "daily_summary.md"
SHALLOW_NOTES_DIR = OUTPUT_DIR / "shallow_notes"
DEEP_NOTES_DIR = OUTPUT_DIR / "deep_notes"

DEFAULT_CACHE = {
    "title_screening": {},
    "shallow_summaries": {},
    "deep_summaries": {},
    "last_ranked": [],
    "last_saved_collection": {},
    "last_shallow_collection": {},
    "last_zotero_listing": {},
}


def render_progress(label: str, current: int, total: int, detail: str = "") -> None:
    total = max(total, 1)
    current = min(max(current, 0), total)
    width = min(24, max(10, shutil.get_terminal_size((80, 20)).columns - len(label) - len(detail) - 20))
    filled = int(width * current / total)
    bar = "#" * filled + "-" * (width - filled)
    line = f"\r{label} [{bar}] {current}/{total}"
    if detail:
        line += f" {detail}"
    sys.stdout.write(line.ljust(shutil.get_terminal_size((80, 20)).columns - 1))
    sys.stdout.flush()
    if current >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()


def usage_total(usage: Dict) -> int:
    return int(usage.get("total_tokens", 0) or 0)


def usage_detail(usage: Dict) -> str:
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", 0) or 0)
    return f"last={total_tokens}t (p{prompt_tokens}/c{completion_tokens})"


def parse_screened_indices(text: str, max_index: int) -> List[int]:
    first_line = (text or "").strip().splitlines()[0].strip()
    if not first_line or first_line.upper() == "NONE":
        return []

    indices = []
    seen = set()
    for token in re.split(r"[\s,]+", first_line):
        if not token:
            continue
        if not token.isdigit():
            continue
        value = int(token)
        if 1 <= value <= max_index and value not in seen:
            indices.append(value)
            seen.add(value)
    return indices


def ensure_directories() -> None:
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SHALLOW_NOTES_DIR.mkdir(parents=True, exist_ok=True)
    DEEP_NOTES_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT / "pdfs").mkdir(parents=True, exist_ok=True)
    if not CACHE_PATH.exists():
        CACHE_PATH.write_text(json.dumps(DEFAULT_CACHE, indent=2), encoding="utf-8")
    if not SEEN_IDS_PATH.exists():
        SEEN_IDS_PATH.write_text(json.dumps({"seen_identifiers": []}, indent=2), encoding="utf-8")


def _with_cache_defaults(cache: Dict) -> Dict:
    merged = dict(DEFAULT_CACHE)
    merged.update(cache or {})
    return merged


def load_cache() -> Dict:
    ensure_directories()
    return _with_cache_defaults(json.loads(CACHE_PATH.read_text(encoding="utf-8")))


def save_cache(cache: Dict) -> None:
    CACHE_PATH.write_text(json.dumps(_with_cache_defaults(cache), indent=2, ensure_ascii=False), encoding="utf-8")


def load_seen_identifiers() -> Set[str]:
    ensure_directories()
    payload = json.loads(SEEN_IDS_PATH.read_text(encoding="utf-8"))
    return set(payload.get("seen_identifiers", []))


def save_seen_identifiers(seen_identifiers: Set[str]) -> None:
    SEEN_IDS_PATH.write_text(
        json.dumps({"seen_identifiers": sorted(seen_identifiers)}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def normalize_title(title: str) -> str:
    return " ".join(title.lower().strip().split())


def normalized_journal_targets() -> List[str]:
    if isinstance(TARGET_JOURNALS, list):
        return [normalize_title(str(value)) for value in TARGET_JOURNALS if normalize_title(str(value))]
    if isinstance(TARGET_JOURNALS, str) and normalize_title(TARGET_JOURNALS):
        return [normalize_title(TARGET_JOURNALS)]
    return []


def paper_identifier(paper: Dict) -> str:
    identifier = (paper.get("identifier") or "").strip().lower()
    if identifier:
        return identifier
    doi = (paper.get("doi") or "").strip().lower()
    if doi:
        return f"doi:{doi}"
    url = (paper.get("url") or "").strip().lower()
    source = (paper.get("source") or "paper").strip().lower()
    return f"{source}:{url}" if url else ""


def deduplicate_papers(papers: List[Dict]) -> List[Dict]:
    deduped = {}
    for paper in papers:
        key = normalize_title(paper.get("title", ""))
        if key and key not in deduped:
            deduped[key] = paper
    return list(deduped.values())


def filter_seen_papers(papers: List[Dict], seen_identifiers: Set[str]) -> List[Dict]:
    fresh = []
    for paper in papers:
        identifier = paper_identifier(paper)
        if identifier and identifier in seen_identifiers:
            continue
        fresh.append(paper)
    return fresh


def remember_papers(papers: List[Dict], seen_identifiers: Set[str]) -> None:
    for paper in papers:
        identifier = paper_identifier(paper)
        if identifier:
            seen_identifiers.add(identifier)


def journal_priority_key(paper: Dict) -> Tuple[int, int, str]:
    targets = normalized_journal_targets()
    if not targets:
        return (999, 999, normalize_title(paper.get("title", "")))

    if paper.get("source") != "PubMed":
        return (999, 999, normalize_title(paper.get("title", "")))

    journal = normalize_title(paper.get("journal", ""))
    for index, target in enumerate(targets):
        if target == journal or target in journal or journal in target:
            return (0, index, normalize_title(paper.get("title", "")))
    return (1, 999, normalize_title(paper.get("title", "")))


def fetch_all_papers(seen_identifiers: Set[str], per_source_limit: int) -> Tuple[List[Dict], Dict]:
    fetchers = [("bioRxiv", get_biorxiv), ("arXiv", get_arxiv)]
    if RUN_PUBMED:
        fetchers.append(("PubMed", get_pubmed))

    papers = []
    source_counts = {}
    total_sources = len(fetchers)
    render_progress("Fetch", 0, total_sources, "starting")
    for index, (name, fetcher) in enumerate(fetchers, start=1):
        source_papers = fetcher(limit=per_source_limit)
        papers.extend(source_papers)
        source_counts[name] = len(source_papers)
        render_progress("Fetch", index, total_sources, f"{name}:{len(source_papers)}")

    deduped = deduplicate_papers(papers)
    fresh = filter_seen_papers(deduped, seen_identifiers)
    stats = {
        "per_source_limit": per_source_limit,
        "fetched_total": len(papers),
        "deduped_total": len(deduped),
        "fresh_total": len(fresh),
        "seen_filtered": len(deduped) - len(fresh),
        "source_counts": source_counts,
    }
    return fresh, stats


def collect_source_candidates(seen_identifiers: Set[str], target_per_source: Dict[str, int]) -> Tuple[List[Dict], Dict]:
    fetchers = [("bioRxiv", get_biorxiv), ("arXiv", get_arxiv)]
    if RUN_PUBMED:
        fetchers.append(("PubMed", get_pubmed))

    candidates: List[Dict] = []
    source_counts: Dict[str, int] = {}
    source_errors: Dict[str, str] = {}
    total_sources = len(fetchers)
    render_progress("Fetch", 0, total_sources, "starting")

    for index, (name, fetcher) in enumerate(fetchers, start=1):
        source_selected: List[Dict] = []
        source_target = max(0, int(target_per_source.get(name, 0)))
        per_source_limit = max(source_target * 5, source_target) if source_target > 0 else 0
        try:
            if source_target > 0:
                fetched = fetcher(limit=per_source_limit)
                deduped = deduplicate_papers(fetched)
                fresh = filter_seen_papers(deduped, seen_identifiers)
                source_selected = fresh[:source_target]
        except Exception as exc:
            source_errors[name] = f"{exc.__class__.__name__}"
            source_selected = []

        candidates.extend(source_selected)
        source_counts[name] = len(source_selected)
        detail = f"{name}:{len(source_selected)}"
        if name in source_errors:
            detail = f"{name}:ERR"
        render_progress("Fetch", index, total_sources, detail)

    stats = {
        "candidate_total": len(candidates),
        "source_counts": source_counts,
        "source_errors": source_errors,
        "target_per_source": target_per_source,
    }
    return candidates, stats


def build_ranked_papers(seen_identifiers: Set[str]) -> Tuple[List[Dict], Dict]:
    cache = load_cache()
    title_cache = cache.get("title_screening", {})
    target_per_source = {
        "bioRxiv": TITLE_SCREEN_BIORXIV,
        "arXiv": TITLE_SCREEN_ARXIV,
        "PubMed": TITLE_SCREEN_PUBMED,
    }
    candidates, stats = collect_source_candidates(seen_identifiers, target_per_source=target_per_source)
    if normalized_journal_targets():
        ordered_candidates = [paper for _, paper in sorted(enumerate(candidates), key=lambda item: (journal_priority_key(item[1]), item[0]))]
    else:
        ordered_candidates = list(candidates)

    if not RESEARCH_DIRECTION.strip():
        best_ranked = ordered_candidates[:TOP_K]
        remember_papers(best_ranked, seen_identifiers)
        cache["title_screening"] = title_cache
        save_cache(cache)
        stats["screened_total"] = 0
        stats["accepted_total"] = len(best_ranked)
        stats["screening_tokens"] = 0
        stats["ranked_total"] = len(best_ranked)
        stats["ranking_mode"] = "journal_priority" if normalized_journal_targets() else "source_order"
        return best_ranked, stats

    cached_ranked: List[Dict] = []
    to_screen: List[Dict] = []
    for paper in ordered_candidates:
        title_key = normalize_title(paper["title"])
        cached_decision = title_cache.get(title_key)
        if cached_decision is None:
            to_screen.append(paper)
            continue
        if cached_decision.get("accepted"):
            cached_ranked.append(dict(paper))

    total_tokens = 0
    if to_screen:
        render_progress("Title LLM", 0, 1, "waiting")
        result = screen_titles_batch_result(to_screen)
        selected_indices = parse_screened_indices(result["content"], len(to_screen))
        total_tokens += usage_total(result.get("usage", {}))
        render_progress("Title LLM", 1, 1, f"{usage_detail(result.get('usage', {}))} total={total_tokens}t")

        selected_index_set = set(selected_indices)
        for idx, paper in enumerate(to_screen, start=1):
            title_key = normalize_title(paper["title"])
            accepted = idx in selected_index_set
            title_cache[title_key] = {"accepted": accepted}
            if accepted:
                cached_ranked.append(dict(paper))

    # Preserve input order and cap at TOP_K for downstream shallow selection.
    deduped_ranked = []
    seen_titles = set()
    for paper in cached_ranked:
        title_key = normalize_title(paper["title"])
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        deduped_ranked.append(paper)

    best_ranked = deduped_ranked[:TOP_K]
    cache["title_screening"] = title_cache
    save_cache(cache)
    remember_papers(best_ranked, seen_identifiers)

    stats["screened_total"] = len(to_screen)
    stats["accepted_total"] = len(best_ranked)
    stats["screening_tokens"] = total_tokens
    stats["ranked_total"] = len(best_ranked)
    stats["ranking_mode"] = "journal_priority+llm" if normalized_journal_targets() else "llm"
    return best_ranked, stats


def get_shallow_summary(paper: Dict, cache: Dict) -> Dict:
    key = normalize_title(paper["title"])
    cached = cache["shallow_summaries"].get(key)
    if cached:
        return {"content": cached, "usage": {}, "cached": True}

    result = summarize_shallow_result(paper)
    if result["content"].strip():
        cache["shallow_summaries"][key] = result["content"]
    result["cached"] = False
    return result


def get_deep_summary(paper: Dict, cache: Dict) -> Dict:
    key = normalize_title(paper["title"])
    cached = cache["deep_summaries"].get(key)
    if cached:
        return {"content": cached, "usage": {}, "cached": True}

    result = summarize_deep_result(paper)
    if result["content"].strip():
        cache["deep_summaries"][key] = result["content"]
    result["cached"] = False
    return result


def write_listing_markdown(papers: List[Dict]) -> None:
    lines = ["# Paper Titles", ""]
    for index, paper in enumerate(papers, start=1):
        lines.append(f"## {index}. {paper['title']}")
        lines.append(f"- Source: {paper.get('source', '')}")
        lines.append(f"- URL: {paper.get('url', '')}")
        lines.append("")
    DAILY_SUMMARY_PATH.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def format_shallow_markdown(collection_name: str, entries: List[Tuple[int, Dict, str]]) -> str:
    lines = [f"# Shallow Summaries - {collection_name}", ""]
    for index, paper, summary in entries:
        lines.append(f"## {index}. {paper['title']}")
        lines.append("")
        lines.append(f"- Source: {paper.get('source', '')}")
        lines.append(f"- URL: {paper.get('url', '')}")
        lines.extend(summary.splitlines())
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def format_deep_markdown(collection_key: str, entries: List[Tuple[int, Dict, str]]) -> str:
    lines = [f"# Deep Summaries - {collection_key}", ""]
    for index, paper, summary in entries:
        lines.append(f"## {index}. {paper['title']}")
        lines.append("")
        lines.append(f"- URL: {paper.get('url', '')}")
        lines.append(summary)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def print_ranked_titles(papers: List[Dict], heading: str) -> None:
    print(heading)
    if not papers:
        print("No papers found.")
        return
    for index, paper in enumerate(papers, start=1):
        print(f"{index}. {paper['title']} [{paper.get('source', '')}]")


def parse_selection_text(raw_text: str, max_index: int) -> List[int]:
    tokens = raw_text.replace(",", " ").split()
    if not tokens:
        raise ValueError("No indices provided.")

    selections = []
    seen = set()
    for token in tokens:
        value = int(token)
        if value < 1 or value > max_index:
            raise ValueError(f"Index {value} is out of range 1..{max_index}")
        if value not in seen:
            selections.append(value)
            seen.add(value)
    return selections


def resolve_indices(indices: Optional[List[int]], max_index: int, prompt_label: str) -> List[int]:
    if max_index <= 0:
        raise RuntimeError("No papers available.")
    if indices:
        return parse_selection_text(" ".join(str(index) for index in indices), max_index)

    raw_text = input(f"请输入要做{prompt_label}的序号，空格分隔: ").strip()
    return parse_selection_text(raw_text, max_index)


def resolve_ranked_papers(cache: Dict) -> List[Dict]:
    cached_ranked = cache.get("last_ranked", [])
    if cached_ranked:
        return cached_ranked

    seen_identifiers = load_seen_identifiers()
    ranked, _ = build_ranked_papers(seen_identifiers)
    save_seen_identifiers(seen_identifiers)
    cache["last_ranked"] = ranked
    return ranked


def get_collection_key(cache: Dict, collection_key: str = "") -> str:
    if collection_key:
        return collection_key
    last_saved_collection = cache.get("last_saved_collection", {})
    saved_key = last_saved_collection.get("key", "")
    if saved_key:
        return saved_key
    last_collection = cache.get("last_shallow_collection", {})
    key = last_collection.get("key", "")
    if not key:
        raise RuntimeError("No Zotero subcollection specified and no recent shallow collection found in cache.")
    return key


def resolve_collection_items(cache: Dict, collection_key: str) -> List[Dict]:
    last_listing = cache.get("last_zotero_listing", {})
    if last_listing.get("collection_key") == collection_key and last_listing.get("items"):
        return last_listing["items"]

    items = list_collection_items(collection_key)
    status = get_collection_local_status(collection_key)
    cache["last_zotero_listing"] = {
        "collection_key": collection_key,
        "collection_name": status.get("collection_name", collection_key),
        "items": items,
    }
    save_cache(cache)
    return items


def download_pdf_safe(paper: Dict) -> Optional[str]:
    try:
        return download_pdf(paper)
    except Exception:
        return None


def write_note_file(directory: Path, stem: str, content: str) -> Path:
    target = directory / f"{safe_filename(stem)}.md"
    target.write_text(content, encoding="utf-8")
    return target


def print_generated_entries(entries: List[Tuple[int, Dict, str]]) -> None:
    for index, paper, summary in entries:
        print("")
        print(f"## {index}. {paper['title']}")
        print(summary)


def run_doi_import(raw_dois: Optional[List[str]]) -> None:
    cache = load_cache()
    seen_identifiers = load_seen_identifiers()

    if raw_dois:
        doi_text = " ".join(raw_dois)
    else:
        doi_text = input("请输入 DOI，支持空格、逗号或换行分隔: ").strip()

    dois = normalize_doi_text(doi_text)
    if not dois:
        raise RuntimeError("No DOI provided.")

    render_progress("DOI Fetch", 0, len(dois), "starting")
    papers, errors = fetch_papers_by_dois(dois)
    detail = f"ok={len(papers)} err={len(errors)}"
    render_progress("DOI Fetch", len(dois), len(dois), detail)

    render_progress("PDF", 0, max(len(papers), 1), "waiting")
    for index, paper in enumerate(papers, start=1):
        pdf_path = download_pdf_safe(paper)
        if pdf_path:
            paper["local_pdf_path"] = pdf_path
        render_progress("PDF", index, len(papers), paper["title"][:24])

    cache["last_ranked"] = papers
    save_cache(cache)
    remember_papers(papers, seen_identifiers)
    save_seen_identifiers(seen_identifiers)
    write_listing_markdown(papers)

    if errors:
        error_text = ", ".join(f"{doi}:{error}" for doi, error in errors)
        print(f"\nDOI errors: {error_text}")
    print_ranked_titles(papers, "\nDOI 导入结果:")
    if papers:
        print("\n下一步可运行: python3 pipeline.py --shallow 1 2")


def run_fetch_list() -> None:
    cache = load_cache()
    seen_identifiers = load_seen_identifiers()
    ranked, stats = build_ranked_papers(seen_identifiers)
    cache["last_ranked"] = ranked
    save_cache(cache)
    save_seen_identifiers(seen_identifiers)
    write_listing_markdown(ranked)
    summary = (
        f"\n统计: candidates={stats['candidate_total']}, "
        f"screened={stats['screened_total']}, "
        f"accepted={stats['accepted_total']}, "
        f"ranked={stats['ranked_total']}"
    )
    if stats.get("source_counts"):
        source_text = ", ".join(f"{name}:{count}" for name, count in stats["source_counts"].items())
        summary += f" [{source_text}]"
    if stats.get("source_errors"):
        error_text = ", ".join(f"{name}:{error}" for name, error in stats["source_errors"].items())
        summary += f" errors=({error_text})"
    print(summary)
    print_ranked_titles(ranked, "\n检索结果:")
    if ranked:
        print("\n下一步可运行: python3 pipeline.py --shallow 1 3")


def run_shallow(indices: Optional[List[int]]) -> None:
    cache = load_cache()
    ranked = resolve_ranked_papers(cache)
    save_cache(cache)
    print_ranked_titles(ranked, "\n可选论文:")
    selected_indices = resolve_indices(indices, len(ranked), "浅摘要")

    run_collection = create_run_collection()
    selected_entries: List[Tuple[int, Dict, str]] = []
    total_tokens = 0
    render_progress("Shallow LLM", 0, len(selected_indices), "waiting")
    for position, index in enumerate(selected_indices, start=1):
        paper = ranked[index - 1]
        result = get_shallow_summary(paper, cache)
        summary = result["content"]
        if result.get("cached"):
            detail = f"cached total={total_tokens}t"
        else:
            total_tokens += usage_total(result.get("usage", {}))
            detail = f"{usage_detail(result.get('usage', {}))} total={total_tokens}t"
        render_progress("Shallow LLM", position, len(selected_indices), detail)
        selected_entries.append((index, paper, summary))

    markdown = format_shallow_markdown(run_collection["name"], selected_entries)
    note_path = write_note_file(SHALLOW_NOTES_DIR, run_collection["name"], markdown)

    total_zotero_steps = len(selected_entries) + 1
    render_progress("Zotero", 0, total_zotero_steps, run_collection["name"])
    add_collection_note(run_collection["key"], f"Shallow Summaries {run_collection['name']}", markdown)
    render_progress("Zotero", 1, total_zotero_steps, "markdown")
    for position, (_, paper, summary) in enumerate(selected_entries, start=2):
        pdf_path = download_pdf_safe(paper)
        add_to_zotero(paper, summary, pdf_path, collection_key=run_collection["key"])
        render_progress("Zotero", position, total_zotero_steps, paper["title"][:24])

    cache["last_shallow_collection"] = run_collection
    save_cache(cache)

    print_generated_entries(selected_entries)
    print(f"\nShallow markdown: {note_path}")
    print(f"Zotero subcollection: {run_collection['name']} ({run_collection['key']})")


def run_save_selected_to_zotero(indices: Optional[List[int]]) -> None:
    cache = load_cache()
    ranked = resolve_ranked_papers(cache)
    save_cache(cache)
    print_ranked_titles(ranked, "\n可选论文:")
    selected_indices = resolve_indices(indices, len(ranked), "保存到 Zotero")

    run_collection = create_run_collection()
    render_progress("Zotero", 0, len(selected_indices), run_collection["name"])
    for position, index in enumerate(selected_indices, start=1):
        paper = ranked[index - 1]
        pdf_path = download_pdf_safe(paper)
        if pdf_path:
            paper["local_pdf_path"] = pdf_path
        add_to_zotero(paper, "", pdf_path, collection_key=run_collection["key"])
        render_progress("Zotero", position, len(selected_indices), paper["title"][:24])

    cache["last_saved_collection"] = run_collection
    save_cache(cache)
    print(f"\nZotero subcollection: {run_collection['name']} ({run_collection['key']})")


def run_zotero_shallow(collection_key: str = "", indices: Optional[List[int]] = None) -> None:
    cache = load_cache()
    target_key = get_collection_key(cache, collection_key)
    items = resolve_collection_items(cache, target_key)
    print_ranked_titles(items, f"\nZotero 子文件夹 {target_key}:")
    selected_indices = resolve_indices(indices, len(items), "浅摘要")

    selected_entries: List[Tuple[int, Dict, str]] = []
    total_tokens = 0
    render_progress("Shallow LLM", 0, len(selected_indices), "waiting")
    for position, index in enumerate(selected_indices, start=1):
        paper = items[index - 1]
        result = get_shallow_summary(paper, cache)
        summary = result["content"]
        if result.get("cached"):
            detail = f"cached total={total_tokens}t"
        else:
            total_tokens += usage_total(result.get("usage", {}))
            detail = f"{usage_detail(result.get('usage', {}))} total={total_tokens}t"
        render_progress("Shallow LLM", position, len(selected_indices), detail)
        selected_entries.append((index, paper, summary))

    collection_name = target_key
    listing = cache.get("last_zotero_listing", {})
    if listing.get("collection_key") == target_key:
        collection_name = listing.get("collection_name", target_key) or target_key
    markdown = format_shallow_markdown(collection_name, selected_entries)
    note_path = write_note_file(SHALLOW_NOTES_DIR, collection_name, markdown)

    total_zotero_steps = len(selected_entries) + 1
    render_progress("Zotero", 0, total_zotero_steps, target_key)
    add_collection_note(target_key, f"Shallow Summaries {collection_name}", markdown)
    render_progress("Zotero", 1, total_zotero_steps, "markdown")
    for position, (_, paper, summary) in enumerate(selected_entries, start=2):
        add_note_to_existing_item(paper["item_key"], summary)
        render_progress("Zotero", position, total_zotero_steps, paper["title"][:24])

    cache["last_shallow_collection"] = {"name": collection_name, "key": target_key}
    save_cache(cache)
    print_generated_entries(selected_entries)
    print(f"\nShallow markdown: {note_path}")


def run_zotero_list(collection_key: str = "") -> None:
    cache = load_cache()
    target_key = get_collection_key(cache, collection_key)
    items = resolve_collection_items(cache, target_key)
    print_ranked_titles(items, f"\nZotero 子文件夹 {target_key}:")
    if items:
        print(f"\n下一步可运行: python3 pipeline.py --zotero-deep {target_key} 1 2")


def parse_zotero_deep_args(tokens: Optional[List[str]]) -> Tuple[str, Optional[List[int]]]:
    if not tokens:
        return "", None
    if tokens[0].isdigit():
        return "", [int(token) for token in tokens]
    collection_key = tokens[0]
    indices = [int(token) for token in tokens[1:]] if len(tokens) > 1 else None
    return collection_key, indices


def parse_collection_indices_args(tokens: Optional[List[str]]) -> Tuple[str, Optional[List[int]]]:
    if not tokens:
        return "", None
    if tokens[0].isdigit():
        return "", [int(token) for token in tokens]
    collection_key = tokens[0]
    indices = [int(token) for token in tokens[1:]] if len(tokens) > 1 else None
    return collection_key, indices


def run_zotero_deep(collection_key: str = "", indices: Optional[List[int]] = None) -> None:
    cache = load_cache()
    target_key = get_collection_key(cache, collection_key)
    items = resolve_collection_items(cache, target_key)
    print_ranked_titles(items, f"\nZotero 子文件夹 {target_key}:")
    selected_indices = resolve_indices(indices, len(items), "深摘要")

    selected_entries: List[Tuple[int, Dict, str]] = []
    total_tokens = 0
    render_progress("Deep LLM", 0, len(selected_indices), "waiting")
    for position, index in enumerate(selected_indices, start=1):
        paper = items[index - 1]
        result = get_deep_summary(paper, cache)
        note = result["content"]
        if result.get("cached"):
            detail = f"cached total={total_tokens}t"
        else:
            total_tokens += usage_total(result.get("usage", {}))
            detail = f"{usage_detail(result.get('usage', {}))} total={total_tokens}t"
        render_progress("Deep LLM", position, len(selected_indices), detail)
        selected_entries.append((index, paper, note))

    markdown = format_deep_markdown(target_key, selected_entries)
    note_path = write_note_file(DEEP_NOTES_DIR, f"{target_key}_deep", markdown)

    total_zotero_steps = len(selected_entries) + 1
    render_progress("Zotero", 0, total_zotero_steps, target_key)
    add_collection_note(target_key, f"Deep Summaries {target_key}", markdown)
    render_progress("Zotero", 1, total_zotero_steps, "markdown")
    for position, (_, paper, note) in enumerate(selected_entries, start=2):
        add_note_to_existing_item(paper["item_key"], note)
        render_progress("Zotero", position, total_zotero_steps, paper["title"][:24])

    save_cache(cache)

    print_generated_entries(selected_entries)
    print(f"\nDeep markdown: {note_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper retrieval and Zotero workflow")
    parser.add_argument("--doi", nargs="*", help="Import papers from DOI list and prepare shallow-summary candidates")
    parser.add_argument("--save-zotero", nargs="*", type=int, help="Save selected fetched papers and PDFs to a new Zotero subcollection")
    parser.add_argument("--shallow", nargs="*", type=int, help="Selected indices from the latest retrieval list")
    parser.add_argument(
        "--zotero-shallow",
        nargs="*",
        help="Run shallow summary for local Zotero items. Usage: --zotero-shallow <collection_key> 1 3 or --zotero-shallow 1 3",
    )
    parser.add_argument("--zotero-list", nargs="?", const="", help="List paper titles in a Zotero subcollection")
    parser.add_argument(
        "--zotero-deep",
        nargs="*",
        help="Run deep summary for Zotero items. Usage: --zotero-deep <collection_key> 1 3 or --zotero-deep 1 3",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.doi is not None:
        run_doi_import(args.doi or None)
        return
    if args.save_zotero is not None:
        run_save_selected_to_zotero(args.save_zotero or None)
        return
    if args.shallow is not None:
        run_shallow(args.shallow or None)
        return
    if args.zotero_shallow is not None:
        collection_key, indices = parse_collection_indices_args(args.zotero_shallow)
        run_zotero_shallow(collection_key, indices)
        return
    if args.zotero_list is not None:
        run_zotero_list(args.zotero_list or "")
        return
    if args.zotero_deep is not None:
        collection_key, indices = parse_zotero_deep_args(args.zotero_deep)
        run_zotero_deep(collection_key, indices)
        return
    run_fetch_list()


if __name__ == "__main__":
    main()
