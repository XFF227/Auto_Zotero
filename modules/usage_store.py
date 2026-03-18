import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict


USAGE_PATH = Path(__file__).resolve().parent.parent / "data" / "usage_stats.json"


def _ensure_file() -> None:
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not USAGE_PATH.exists():
        USAGE_PATH.write_text(json.dumps({"keys": {}}, indent=2), encoding="utf-8")


def _load_payload() -> Dict:
    _ensure_file()
    return json.loads(USAGE_PATH.read_text(encoding="utf-8"))


def _save_payload(payload: Dict) -> None:
    _ensure_file()
    USAGE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def api_key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def record_usage(api_key: str, model: str, usage: Dict) -> None:
    if not api_key:
        return

    payload = _load_payload()
    key_hash = api_key_hash(api_key)
    keys = payload.setdefault("keys", {})
    key_bucket = keys.setdefault(
        key_hash,
        {
            "models": {},
            "updated_at": "",
        },
    )
    model_bucket = key_bucket["models"].setdefault(
        model,
        {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
        },
    )

    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", 0) or 0)
    prompt_details = usage.get("prompt_tokens_details", {}) or {}
    cached_tokens = int(prompt_details.get("cached_tokens", 0) or 0)

    model_bucket["prompt_tokens"] += prompt_tokens
    model_bucket["completion_tokens"] += completion_tokens
    model_bucket["cached_tokens"] += cached_tokens
    model_bucket["total_tokens"] += total_tokens
    key_bucket["updated_at"] = datetime.now(timezone.utc).isoformat()

    _save_payload(payload)


def get_usage_for_key(api_key: str) -> Dict:
    if not api_key:
        return {"models": {}, "updated_at": ""}
    payload = _load_payload()
    key_hash = api_key_hash(api_key)
    return payload.get("keys", {}).get(key_hash, {"models": {}, "updated_at": ""})
