import ast
from pathlib import Path
from typing import Dict


CONFIG_KEYS = [
    "OPENAI_API_KEY",
    "MODEL_SUMMARY",
    "MODEL_DEEP",
    "ZOTERO_API_KEY",
    "ZOTERO_USER_ID",
    "ZOTERO_COLLECTION",
    "ZOTERO_LOCAL_DIR",
    "RESEARCH_DIRECTION",
    "TITLE_SCREEN_BIORXIV",
    "TITLE_SCREEN_ARXIV",
    "TITLE_SCREEN_PUBMED",
    "TARGET_JOURNALS",
    "TOP_K",
    "FETCH_LOOKBACK_DAYS",
    "RUN_PUBMED",
]


def _config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.py"


def load_config_values() -> Dict:
    config_path = _config_path()
    tree = ast.parse(config_path.read_text(encoding="utf-8"))
    values = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        key = target.id
        if key not in CONFIG_KEYS:
            continue
        values[key] = ast.literal_eval(node.value)
    return values


def save_config_values(values: Dict) -> None:
    config_path = _config_path()
    lines = []
    for key in CONFIG_KEYS:
        value = values[key]
        if isinstance(value, str):
            rendered = repr(value)
        else:
            rendered = repr(value)
        lines.append(f"{key} = {rendered}")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
