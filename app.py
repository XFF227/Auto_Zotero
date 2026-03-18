import importlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

import streamlit as st

from modules.config_store import CONFIG_KEYS, load_config_values, save_config_values
from modules.usage_store import get_usage_for_key


ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "data" / "cache.json"
SHALLOW_NOTES_DIR = ROOT / "output" / "shallow_notes"
DEEP_NOTES_DIR = ROOT / "output" / "deep_notes"


st.set_page_config(page_title="Paper Workflow", layout="wide")


def load_cache() -> Dict:
    if not CACHE_PATH.exists():
        return {}
    return json.loads(CACHE_PATH.read_text(encoding="utf-8"))


def clear_business_cache() -> None:
    payload = load_cache()
    payload["title_screening"] = {}
    payload["shallow_summaries"] = {}
    payload["deep_summaries"] = {}
    CACHE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    for key in [
        "listed_items",
        "collection_status",
        "listed_shallow_items",
        "shallow_collection_status",
    ]:
        st.session_state.pop(key, None)


TOKEN_PATTERN = re.compile(r"total=(\d+)t")
PRICING_PER_1M = {
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-pro": {"input": 30.00, "cached_input": None, "output": 180.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.02, "output": 1.25},
}


def _extract_token_total(text: str) -> int:
    matches = TOKEN_PATTERN.findall(text)
    if not matches:
        return 0
    return max(int(match) for match in matches)


def normalize_pricing_model(model: str) -> str:
    if model in PRICING_PER_1M:
        return model
    for base_model in sorted(PRICING_PER_1M.keys(), key=len, reverse=True):
        if model == base_model or model.startswith(base_model + "-"):
            return base_model
    return model


def calculate_usage_summary(api_key: str) -> Dict:
    usage = get_usage_for_key(api_key)
    models = usage.get("models", {})
    prompt_total = 0
    completion_total = 0
    cached_total = 0
    total_total = 0
    cost_total = 0.0
    unknown_models = []

    for model, stats in models.items():
        pricing_model = normalize_pricing_model(model)
        prompt_tokens = int(stats.get("prompt_tokens", 0) or 0)
        completion_tokens = int(stats.get("completion_tokens", 0) or 0)
        cached_tokens = int(stats.get("cached_tokens", 0) or 0)
        total_tokens = int(stats.get("total_tokens", 0) or 0)

        prompt_total += prompt_tokens
        completion_total += completion_tokens
        cached_total += cached_tokens
        total_total += total_tokens

        pricing = PRICING_PER_1M.get(pricing_model)
        if not pricing:
            unknown_models.append(model)
            continue

        effective_prompt_tokens = max(prompt_tokens - cached_tokens, 0)
        cached_rate = pricing["cached_input"] if pricing["cached_input"] is not None else pricing["input"]
        cost_total += effective_prompt_tokens / 1_000_000 * pricing["input"]
        cost_total += cached_tokens / 1_000_000 * cached_rate
        cost_total += completion_tokens / 1_000_000 * pricing["output"]

    return {
        "prompt_tokens": prompt_total,
        "completion_tokens": completion_total,
        "cached_tokens": cached_total,
        "total_tokens": total_total,
        "cost_usd": cost_total,
        "unknown_models": unknown_models,
        "updated_at": usage.get("updated_at", ""),
    }


def render_usage_metrics(container, api_key: str) -> None:
    summary = calculate_usage_summary(api_key)
    note = ""
    if summary["unknown_models"]:
        note = f"<div style='font-size:12px;color:#6b7280;margin-top:4px;'>未定价模型: {', '.join(summary['unknown_models'])}</div>"
    html = f"""
    <div style="
        border:1px solid #e5e7eb;
        border-radius:10px;
        padding:10px 12px;
        background:#fafafa;
        text-align:right;
        line-height:1.2;
    ">
        <div style="font-size:12px;color:#6b7280;">当前 API Key 累计</div>
        <div style="font-size:15px;font-weight:600;margin-top:4px;">{summary['total_tokens']} tokens</div>
        <div style="font-size:15px;font-weight:600;margin-top:2px;">${summary['cost_usd']:.4f}</div>
        {note}
    </div>
    """
    container.markdown(html, unsafe_allow_html=True)


def run_command_live(args: List[str], title: str, usage_container, api_key: str) -> Dict:
    status_placeholder = st.empty()
    output_placeholder = st.empty()

    process = subprocess.Popen(
        [sys.executable, *args],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    status_placeholder.info(f"{title} 运行中")
    render_usage_metrics(usage_container, api_key)

    transcript = ""
    line_buffer = ""
    rendered_lines: List[str] = []

    while True:
        chunk = process.stdout.read(1) if process.stdout else ""
        if chunk == "" and process.poll() is not None:
            if line_buffer:
                rendered_lines.append(line_buffer)
                line_buffer = ""
            break
        if not chunk:
            continue

        transcript += chunk
        if chunk == "\r":
            if rendered_lines:
                rendered_lines[-1] = line_buffer
            elif line_buffer:
                rendered_lines.append(line_buffer)
            line_buffer = ""
        elif chunk == "\n":
            rendered_lines.append(line_buffer)
            line_buffer = ""
        else:
            line_buffer += chunk

        render_usage_metrics(usage_container, api_key)
        visible = "\n".join(rendered_lines[-40:] + ([line_buffer] if line_buffer else []))
        if visible.strip():
            output_placeholder.code(visible)

    returncode = process.wait()
    output = transcript.strip()
    token_total = _extract_token_total(transcript)
    render_usage_metrics(usage_container, api_key)

    return {
        "returncode": returncode,
        "output": output,
        "token_total": token_total,
        "status_placeholder": status_placeholder,
        "output_placeholder": output_placeholder,
    }


def read_latest_markdown(directory: Path) -> str:
    files = sorted(directory.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        return ""
    return files[0].read_text(encoding="utf-8")


def refresh_zotero_module():
    import config
    import modules.zotero as zotero

    importlib.reload(config)
    importlib.reload(zotero)
    return zotero


def default_config() -> Dict:
    values = load_config_values()
    return {key: values.get(key, "") for key in CONFIG_KEYS}


def show_command_result(result: Dict) -> None:
    status_placeholder = result.get("status_placeholder")
    if result["returncode"] == 0:
        if status_placeholder:
            status_placeholder.success("运行完成")
    else:
        if status_placeholder:
            status_placeholder.error(f"运行失败，退出码 {result['returncode']}")


def indices_to_argument(indices: List[int]) -> List[str]:
    return [str(index) for index in sorted(indices)]


st.title("论文检索与 Zotero 工具")
header_left, header_right = st.columns([3, 2])
with header_left:
    st.caption("本地检索、摘要与 Zotero 导入")
usage_panel = header_right.empty()
render_usage_metrics(usage_panel, load_config_values().get("OPENAI_API_KEY", ""))

with st.sidebar:
    st.header("参数")
    config_values = default_config()
    journal_text = "\n".join(config_values.get("TARGET_JOURNALS", []))
    with st.form("config_form"):
        edited = {}
        edited["OPENAI_API_KEY"] = st.text_input("OPENAI_API_KEY", value=config_values["OPENAI_API_KEY"], type="password")
        edited["MODEL_SUMMARY"] = st.text_input("MODEL_SUMMARY", value=config_values["MODEL_SUMMARY"])
        edited["MODEL_DEEP"] = st.text_input("MODEL_DEEP", value=config_values["MODEL_DEEP"])
        edited["ZOTERO_API_KEY"] = st.text_input("ZOTERO_API_KEY", value=config_values["ZOTERO_API_KEY"], type="password")
        edited["ZOTERO_USER_ID"] = st.text_input("ZOTERO_USER_ID", value=str(config_values["ZOTERO_USER_ID"]))
        edited["ZOTERO_COLLECTION"] = st.text_input("ZOTERO_COLLECTION", value=config_values["ZOTERO_COLLECTION"])
        edited["ZOTERO_LOCAL_DIR"] = st.text_input("ZOTERO_LOCAL_DIR", value=config_values["ZOTERO_LOCAL_DIR"])
        edited["RESEARCH_DIRECTION"] = st.text_area("RESEARCH_DIRECTION", value=config_values["RESEARCH_DIRECTION"], height=140)
        edited["TITLE_SCREEN_BIORXIV"] = st.number_input("TITLE_SCREEN_BIORXIV", min_value=0, max_value=100, value=int(config_values["TITLE_SCREEN_BIORXIV"]), step=1)
        edited["TITLE_SCREEN_ARXIV"] = st.number_input("TITLE_SCREEN_ARXIV", min_value=0, max_value=100, value=int(config_values["TITLE_SCREEN_ARXIV"]), step=1)
        edited["TITLE_SCREEN_PUBMED"] = st.number_input("TITLE_SCREEN_PUBMED", min_value=0, max_value=100, value=int(config_values["TITLE_SCREEN_PUBMED"]), step=1)
        journal_text_input = st.text_area("TARGET_JOURNALS", value=journal_text, height=100, help="仅对 PubMed 生效，每行一个期刊名，按影响力高到低排列")
        edited["TOP_K"] = st.number_input("TOP_K", min_value=1, max_value=50, value=int(config_values["TOP_K"]), step=1)
        edited["FETCH_LOOKBACK_DAYS"] = st.number_input("FETCH_LOOKBACK_DAYS", min_value=1, max_value=30, value=int(config_values["FETCH_LOOKBACK_DAYS"]), step=1)
        edited["RUN_PUBMED"] = st.checkbox("RUN_PUBMED", value=bool(config_values["RUN_PUBMED"]))
        submitted = st.form_submit_button("保存参数")
        if submitted:
            edited["TARGET_JOURNALS"] = [line.strip() for line in journal_text_input.splitlines() if line.strip()]
            save_config_values(edited)
            st.success("config.py 已更新")
    st.divider()
    if st.button("清空业务缓存", use_container_width=True):
        clear_business_cache()
        st.success("已清空标题筛选、浅摘要、深摘要缓存")


tab_fetch, tab_shallow, tab_deep = st.tabs(["1. 检索标题", "2. 浅摘要 + Zotero", "3. Deep Summary"])


with tab_fetch:
    st.subheader("检索并筛选标题")
    doi_input = st.text_area("或直接输入 DOI 列表", height=100, help="支持空格、逗号或换行分隔。导入后会自动尝试下载 PDF，并进入浅摘要候选列表。")
    if st.button("导入 DOI", use_container_width=True, disabled=not doi_input.strip()):
        api_key = load_config_values().get("OPENAI_API_KEY", "")
        doi_args = re.split(r"[\s,;]+", doi_input.strip())
        result = run_command_live(["pipeline.py", "--doi", *[token for token in doi_args if token]], "DOI 导入", usage_panel, api_key)
        show_command_result(result)
    if st.button("开始检索", use_container_width=True):
        api_key = load_config_values().get("OPENAI_API_KEY", "")
        result = run_command_live(["pipeline.py"], "检索标题", usage_panel, api_key)
        show_command_result(result)

    cache = load_cache()
    ranked = cache.get("last_ranked", [])
    if ranked:
        st.markdown("### 当前候选标题")
        options = {f"{idx}. {paper['title']} [{paper.get('source', '')}]": idx for idx, paper in enumerate(ranked, start=1)}
        selected_fetch_labels = st.multiselect("选择要保存到 Zotero 的论文", list(options.keys()), key="fetch_save_selection")
        for idx, paper in enumerate(ranked, start=1):
            st.write(f"{idx}. {paper['title']} [{paper.get('source', '')}]")
        if st.button("保存选中文章到 Zotero", use_container_width=True, disabled=not selected_fetch_labels):
            indices = indices_to_argument([options[label] for label in selected_fetch_labels])
            api_key = load_config_values().get("OPENAI_API_KEY", "")
            result = run_command_live(["pipeline.py", "--save-zotero", *indices], "保存到 Zotero", usage_panel, api_key)
            show_command_result(result)
        last_saved_collection = cache.get("last_saved_collection", {})
        if last_saved_collection:
            st.caption(f"最近一次保存到 Zotero: {last_saved_collection.get('name', '')} ({last_saved_collection.get('key', '')})")
    else:
        st.info("还没有候选标题。先点击“开始检索”。")


with tab_shallow:
    st.subheader("浏览本地 Zotero 子文件夹并生成浅摘要")
    cache = load_cache()
    last_saved_collection = cache.get("last_saved_collection", {})
    last_shallow_collection = cache.get("last_shallow_collection", {})
    default_collection_key = last_saved_collection.get("key", "") or last_shallow_collection.get("key", "")
    zotero = refresh_zotero_module()
    child_collections = zotero.list_local_child_collections()

    collection_options = {}
    for entry in child_collections:
        label = f"{entry['name']} | papers={entry['total_items']} | pdf={entry['pdf_items']} | {entry['key']}"
        collection_options[label] = entry["key"]

    option_labels = list(collection_options.keys())
    default_index = 0
    if default_collection_key:
        for idx, label in enumerate(option_labels):
            if collection_options[label] == default_collection_key:
                default_index = idx
                break

    selected_label = ""
    if option_labels:
        selected_label = st.selectbox("选择本地 Zotero 子文件夹", option_labels, index=default_index, key="shallow_collection_select")

    manual_collection_key = st.text_input("或手动输入 Zotero 子文件夹 Key", value="", key="shallow_manual_key")
    collection_key = manual_collection_key.strip() or (collection_options.get(selected_label, "") if selected_label else default_collection_key)

    listed_items = []
    if st.button("列出本地条目", use_container_width=True, disabled=not collection_key):
        with st.spinner("正在读取本地 Zotero 子文件夹..."):
            collection_status = zotero.get_collection_local_status(collection_key)
            listed_items = zotero.list_collection_items(collection_key)
            st.session_state["listed_shallow_items"] = listed_items
            st.session_state["shallow_collection_status"] = collection_status
        st.success("读取完成")

    listed_items = st.session_state.get("listed_shallow_items", [])
    collection_status = st.session_state.get("shallow_collection_status", {})
    if collection_status:
        if not collection_status.get("exists"):
            st.error("这个 Zotero 子文件夹 Key 在本地库里不存在。")
        else:
            st.caption(
                f"本地子文件夹: {collection_status.get('collection_name', '')} | "
                f"条目数: {collection_status.get('total_items', 0)} | "
                f"直接 PDF 数: {collection_status.get('pdf_items', 0)}"
            )
    if listed_items:
        labels = {f"{idx}. {paper['title']}": idx for idx, paper in enumerate(listed_items, start=1)}
        selected_shallow = st.multiselect("选择要做浅摘要的论文", list(labels.keys()), key="shallow_selection")
        if st.button("生成浅摘要并写回 Zotero", use_container_width=True, disabled=not selected_shallow or not collection_key):
            indices = indices_to_argument([labels[label] for label in selected_shallow])
            api_key = load_config_values().get("OPENAI_API_KEY", "")
            result = run_command_live(["pipeline.py", "--zotero-shallow", collection_key, *indices], "本地浅摘要 + Zotero", usage_panel, api_key)
            show_command_result(result)

    shallow_markdown = read_latest_markdown(SHALLOW_NOTES_DIR)
    if shallow_markdown:
        st.markdown("### 最新浅摘要 Markdown")
        st.markdown(shallow_markdown)

    if last_shallow_collection:
        st.caption(f"最近一次浅摘要子文件夹: {last_shallow_collection.get('name', '')} ({last_shallow_collection.get('key', '')})")


with tab_deep:
    st.subheader("列出 Zotero 子文件夹并生成 Deep Summary")
    cache = load_cache()
    last_collection = cache.get("last_shallow_collection", {})
    last_saved_collection = cache.get("last_saved_collection", {})
    default_collection_key = last_collection.get("key", "") or last_saved_collection.get("key", "")
    zotero = refresh_zotero_module()
    child_collections = zotero.list_local_child_collections()

    collection_options = {}
    for entry in child_collections:
        label = f"{entry['name']} | papers={entry['total_items']} | pdf={entry['pdf_items']} | {entry['key']}"
        collection_options[label] = entry["key"]

    option_labels = list(collection_options.keys())
    default_index = 0
    if default_collection_key:
        for idx, label in enumerate(option_labels):
            if collection_options[label] == default_collection_key:
                default_index = idx
                break

    selected_label = ""
    if option_labels:
        selected_label = st.selectbox("选择 Zotero 子文件夹", option_labels, index=default_index)

    manual_collection_key = st.text_input("或手动输入 Zotero 子文件夹 Key", value="")
    collection_key = manual_collection_key.strip() or (collection_options.get(selected_label, "") if selected_label else default_collection_key)

    listed_items = []
    if st.button("列出子文件夹标题", use_container_width=True, disabled=not collection_key):
        with st.spinner("正在读取 Zotero 子文件夹..."):
            collection_status = zotero.get_collection_local_status(collection_key)
            listed_items = zotero.list_collection_items(collection_key)
            st.session_state["listed_items"] = listed_items
            st.session_state["collection_status"] = collection_status
        st.success("读取完成")

    listed_items = st.session_state.get("listed_items", [])
    collection_status = st.session_state.get("collection_status", {})
    if collection_status:
        if not collection_status.get("exists"):
            st.error("这个 Zotero 子文件夹 Key 在本地库里不存在。")
        else:
            st.caption(
                f"本地子文件夹: {collection_status.get('collection_name', '')} | "
                f"条目数: {collection_status.get('total_items', 0)} | "
                f"本地 PDF 数: {collection_status.get('pdf_items', 0)}"
            )
            if collection_status.get("total_items", 0) > 0 and collection_status.get("pdf_items", 0) == 0 and not listed_items:
                st.warning("这个子文件夹里有条目，但当前本地 Zotero library 里没有可用 PDF，所以不会出现 deep 候选列表。")
    if listed_items:
        labels = {f"{idx}. {paper['title']}": idx for idx, paper in enumerate(listed_items, start=1)}
        selected_deep = st.multiselect("选择要做 deep summary 的论文", list(labels.keys()), key="deep_selection")
        if st.button("生成 Deep Summary 并写回 Zotero", use_container_width=True, disabled=not selected_deep or not collection_key):
            indices = indices_to_argument([labels[label] for label in selected_deep])
            api_key = load_config_values().get("OPENAI_API_KEY", "")
            result = run_command_live(["pipeline.py", "--zotero-deep", collection_key, *indices], "Deep Summary + Zotero", usage_panel, api_key)
            show_command_result(result)

    deep_markdown = read_latest_markdown(DEEP_NOTES_DIR)
    if deep_markdown:
        st.markdown("### 最新 Deep Summary Markdown")
        st.markdown(deep_markdown)
