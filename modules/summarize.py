import json
from typing import Dict, List
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from config import MODEL_DEEP, MODEL_SUMMARY, OPENAI_API_KEY, RESEARCH_DIRECTION
from prompts import DEEP_FALLBACK_PROMPT, DEEP_PROMPT, SHALLOW_PROMPT, TITLE_SCREEN_PROMPT
from modules.usage_store import record_usage

MAX_ABSTRACT_CHARS = 2000
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def truncate_abstract(text: str, max_chars: int = MAX_ABSTRACT_CHARS) -> str:
    compact = " ".join((text or "").split())
    return compact[:max_chars]


def _build_user_content(paper: Dict) -> str:
    return (
        f"Title: {paper.get('title', '').strip()}\n"
        f"Source: {paper.get('source', '').strip()}\n"
        f"URL: {paper.get('url', '').strip()}\n"
        f"Abstract: {truncate_abstract(paper.get('abstract', ''))}"
    )


def _build_title_batch_user_content(papers: List[Dict]) -> str:
    lines = [f"Research direction: {RESEARCH_DIRECTION.strip()}", "", "Titles:"]
    for index, paper in enumerate(papers, start=1):
        lines.append(f"{index}. [{paper.get('source', '').strip()}] {paper.get('title', '').strip()}")
    return "\n".join(lines)


def _call_openai_result(prompt: str, paper: Dict, model: str, max_tokens: int) -> Dict:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is empty in config.py")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": _build_user_content(paper)},
        ],
        "temperature": 0.2,
        "max_completion_tokens": max_tokens,
    }
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        OPENAI_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI request failed ({exc.code}): {body}") from exc

    record_usage(OPENAI_API_KEY, result.get("model", model), result.get("usage", {}))

    return {
        "content": result["choices"][0]["message"]["content"].strip(),
        "usage": result.get("usage", {}),
        "model": result.get("model", model),
    }


def _call_openai_with_user_content(prompt: str, user_content: str, model: str, max_tokens: int) -> Dict:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is empty in config.py")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.0,
        "max_completion_tokens": max_tokens,
    }
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        OPENAI_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI request failed ({exc.code}): {body}") from exc

    record_usage(OPENAI_API_KEY, result.get("model", model), result.get("usage", {}))

    return {
        "content": result["choices"][0]["message"]["content"].strip(),
        "usage": result.get("usage", {}),
        "model": result.get("model", model),
    }


def summarize_short(paper: Dict) -> str:
    return summarize_shallow_result(paper)["content"]


def summarize_deep(paper: Dict) -> str:
    return summarize_deep_result(paper)["content"]


def summarize_shallow_result(paper: Dict) -> Dict:
    return _call_openai_result(SHALLOW_PROMPT, paper, MODEL_SUMMARY, max_tokens=220)


def summarize_deep_result(paper: Dict) -> Dict:
    primary = _call_openai_result(DEEP_PROMPT, paper, MODEL_DEEP, max_tokens=1800)
    if primary["content"].strip():
        return primary

    fallback = _call_openai_result(DEEP_FALLBACK_PROMPT, paper, MODEL_DEEP, max_tokens=1400)
    fallback["usage"] = {
        "prompt_tokens": int(primary.get("usage", {}).get("prompt_tokens", 0) or 0)
        + int(fallback.get("usage", {}).get("prompt_tokens", 0) or 0),
        "completion_tokens": int(primary.get("usage", {}).get("completion_tokens", 0) or 0)
        + int(fallback.get("usage", {}).get("completion_tokens", 0) or 0),
        "total_tokens": int(primary.get("usage", {}).get("total_tokens", 0) or 0)
        + int(fallback.get("usage", {}).get("total_tokens", 0) or 0),
    }
    return fallback


def screen_titles_batch_result(papers: List[Dict]) -> Dict:
    if not RESEARCH_DIRECTION.strip():
        raise RuntimeError("RESEARCH_DIRECTION is empty in config.py")
    return _call_openai_with_user_content(
        TITLE_SCREEN_PROMPT,
        _build_title_batch_user_content(papers),
        MODEL_SUMMARY,
        max_tokens=120,
    )
