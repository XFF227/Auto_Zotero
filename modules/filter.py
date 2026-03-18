from typing import Dict, Iterable, List


def _normalized_keywords(keywords: Iterable[str]) -> List[str]:
    return [keyword.strip().lower() for keyword in keywords if keyword and keyword.strip()]


def filter_papers(papers: List[Dict], required_keywords: Iterable[str]) -> List[Dict]:
    normalized = _normalized_keywords(required_keywords)
    if not normalized:
        return papers

    filtered = []
    for paper in papers:
        haystack = f"{paper.get('title', '')} {paper.get('abstract', '')}".lower()
        if all(keyword in haystack for keyword in normalized):
            filtered.append(paper)
    return filtered
