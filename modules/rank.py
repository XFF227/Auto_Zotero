from typing import Dict, Iterable, List


def score_paper(paper: Dict, bonus_keywords: Iterable[str]) -> float:
    normalized_keywords = [keyword.strip().lower() for keyword in bonus_keywords if keyword and keyword.strip()]
    title = paper.get("title", "")
    abstract = paper.get("abstract", "")
    haystack = f"{title} {abstract}".lower()

    keyword_score = sum(2 for keyword in normalized_keywords if keyword in haystack)
    length_score = len(title) / 100.0 + len(abstract) / 500.0
    return round(keyword_score + length_score, 4)


def rank_papers(papers: List[Dict], bonus_keywords: Iterable[str], top_k: int) -> List[Dict]:
    ranked = []
    for paper in papers:
        scored = dict(paper)
        scored["score"] = score_paper(scored, bonus_keywords)
        ranked.append(scored)

    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:top_k]
