import hashlib
import html
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import ZOTERO_API_KEY, ZOTERO_COLLECTION, ZOTERO_LOCAL_DIR, ZOTERO_USER_ID


BASE_URL = "https://api.zotero.org"


def _local_zotero_root() -> Path:
    configured = (ZOTERO_LOCAL_DIR or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Zotero"


def _local_sqlite_path() -> Path:
    return _local_zotero_root() / "zotero.sqlite"


def _local_storage_path() -> Path:
    return _local_zotero_root() / "storage"


def _local_connection() -> sqlite3.Connection:
    sqlite_path = _local_sqlite_path()
    if not sqlite_path.exists():
        raise RuntimeError(f"Local Zotero database not found: {sqlite_path}")
    return sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)


def _resolve_local_attachment_path(attachment_key: str, raw_path: str) -> str:
    raw_value = (raw_path or "").strip()
    if not raw_value:
        return ""
    if raw_value.startswith("storage:"):
        filename = raw_value.split("storage:", 1)[1]
        return str(_local_storage_path() / attachment_key / filename)
    if raw_value.startswith("file://"):
        return raw_value[7:]
    return raw_value


def _normalize_title_key(title: str) -> str:
    return " ".join((title or "").lower().split())


def _build_library_pdf_index(connection: sqlite3.Connection) -> Dict[str, Dict[str, str]]:
    query = """
    WITH article_items AS (
        SELECT i.itemID
        FROM items i
        JOIN itemTypes it ON it.itemTypeID = i.itemTypeID
        WHERE it.typeName IN ('journalArticle', 'preprint', 'report', 'conferencePaper')
    ),
    field_values AS (
        SELECT id.itemID, f.fieldName, idv.value
        FROM itemData id
        JOIN fields f ON f.fieldID = id.fieldID
        JOIN itemDataValues idv ON idv.valueID = id.valueID
        WHERE f.fieldName IN ('title', 'DOI')
    )
    SELECT
        ai_parent.itemID AS parent_item_id,
        MAX(CASE WHEN fv.fieldName = 'title' THEN fv.value END) AS title,
        MAX(CASE WHEN fv.fieldName = 'DOI' THEN fv.value END) AS doi,
        ai_attach.key AS attachment_key,
        ia.path AS attachment_path
    FROM article_items ai_parent
    JOIN itemAttachments ia ON ia.parentItemID = ai_parent.itemID AND ia.contentType = 'application/pdf'
    JOIN items ai_attach ON ai_attach.itemID = ia.itemID
    LEFT JOIN field_values fv ON fv.itemID = ai_parent.itemID
    GROUP BY ai_parent.itemID, ai_attach.key, ia.path
    """

    doi_map: Dict[str, Dict[str, str]] = {}
    title_map: Dict[str, Dict[str, str]] = {}
    for row in connection.execute(query).fetchall():
        title = row["title"] or ""
        doi = (row["doi"] or "").strip().lower()
        attachment_key = row["attachment_key"] or ""
        attachment_path = _resolve_local_attachment_path(attachment_key, row["attachment_path"] or "")
        record = {"attachment_key": attachment_key, "pdf_path": attachment_path}
        if doi and doi not in doi_map:
            doi_map[doi] = record
        title_key = _normalize_title_key(title)
        if title_key and title_key not in title_map:
            title_map[title_key] = record
    return {"doi": doi_map, "title": title_map}


def _list_collection_items_local(collection_key: str) -> List[Dict]:
    query = """
    WITH target_collection AS (
        SELECT collectionID FROM collections WHERE key = ?
    ),
    top_items AS (
        SELECT i.itemID, i.key, it.typeName, i.dateModified
        FROM collectionItems ci
        JOIN items i ON i.itemID = ci.itemID
        JOIN itemTypes it ON it.itemTypeID = i.itemTypeID
        WHERE ci.collectionID = (SELECT collectionID FROM target_collection)
          AND it.typeName IN ('journalArticle', 'preprint', 'report', 'conferencePaper')
    ),
    field_values AS (
        SELECT id.itemID, f.fieldName, idv.value
        FROM itemData id
        JOIN fields f ON f.fieldID = id.fieldID
        JOIN itemDataValues idv ON idv.valueID = id.valueID
        WHERE f.fieldName IN ('title', 'abstractNote', 'url', 'DOI')
    ),
    pdf_attachments AS (
        SELECT ia.parentItemID, ai.key AS attachmentKey, ia.path
        FROM itemAttachments ia
        JOIN items ai ON ai.itemID = ia.itemID
        WHERE ia.parentItemID IS NOT NULL
          AND ia.contentType = 'application/pdf'
    )
    SELECT
        ti.key AS item_key,
        MAX(CASE WHEN fv.fieldName = 'title' THEN fv.value END) AS title,
        MAX(CASE WHEN fv.fieldName = 'abstractNote' THEN fv.value END) AS abstract_note,
        MAX(CASE WHEN fv.fieldName = 'url' THEN fv.value END) AS url,
        MAX(CASE WHEN fv.fieldName = 'DOI' THEN fv.value END) AS doi,
        MAX(pa.attachmentKey) AS attachment_key,
        MAX(pa.path) AS attachment_path,
        ti.typeName,
        ti.dateModified
    FROM top_items ti
    LEFT JOIN field_values fv ON fv.itemID = ti.itemID
    LEFT JOIN pdf_attachments pa ON pa.parentItemID = ti.itemID
    GROUP BY ti.itemID, ti.key, ti.typeName, ti.dateModified
    ORDER BY ti.dateModified DESC
    """

    with _local_connection() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, (collection_key,)).fetchall()
        library_pdf_index = _build_library_pdf_index(connection)

    papers = []
    for row in rows:
        title = (row["title"] or "").strip()
        if not title:
            continue
        direct_attachment_key = row["attachment_key"] or ""
        direct_attachment_path = row["attachment_path"] or ""
        pdf_path = _resolve_local_attachment_path(direct_attachment_key, direct_attachment_path)
        attachment_key = direct_attachment_key
        if not pdf_path:
            doi_key = (row["doi"] or "").strip().lower()
            title_key = _normalize_title_key(title)
            matched = None
            if doi_key:
                matched = library_pdf_index["doi"].get(doi_key)
            if not matched and title_key:
                matched = library_pdf_index["title"].get(title_key)
            if matched:
                attachment_key = matched["attachment_key"]
                pdf_path = matched["pdf_path"]
        if not pdf_path:
            continue
        papers.append(
            {
                "item_key": row["item_key"] or "",
                "title": title,
                "abstract": row["abstract_note"] or "",
                "url": row["url"] or "",
                "source": "Zotero Local",
                "doi": row["doi"] or "",
                "pdf_path": pdf_path,
                "attachment_key": attachment_key,
                "item_type": row["typeName"] or "",
            }
        )
    return papers


def get_collection_local_status(collection_key: str) -> Dict[str, object]:
    query = """
    WITH target_collection AS (
        SELECT collectionID, collectionName FROM collections WHERE key = ?
    ),
    top_items AS (
        SELECT i.itemID
        FROM collectionItems ci
        JOIN items i ON i.itemID = ci.itemID
        JOIN itemTypes it ON it.itemTypeID = i.itemTypeID
        WHERE ci.collectionID = (SELECT collectionID FROM target_collection)
          AND it.typeName IN ('journalArticle', 'preprint', 'report', 'conferencePaper')
    ),
    pdf_items AS (
        SELECT DISTINCT ia.parentItemID
        FROM itemAttachments ia
        WHERE ia.parentItemID IN (SELECT itemID FROM top_items)
          AND ia.contentType = 'application/pdf'
    )
    SELECT
        (SELECT collectionName FROM target_collection) AS collection_name,
        (SELECT COUNT(*) FROM top_items) AS total_items,
        (SELECT COUNT(*) FROM pdf_items) AS pdf_items
    """

    with _local_connection() as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(query, (collection_key,)).fetchone()

    if not row or row["collection_name"] is None:
        return {"exists": False, "collection_name": "", "total_items": 0, "pdf_items": 0}
    return {
        "exists": True,
        "collection_name": row["collection_name"] or "",
        "total_items": int(row["total_items"] or 0),
        "pdf_items": int(row["pdf_items"] or 0),
    }


def list_local_child_collections(parent_collection_key: str = "") -> List[Dict[str, object]]:
    target_key = (parent_collection_key or ZOTERO_COLLECTION or "").strip()
    if not target_key:
        return []

    query = """
    WITH parent_collection AS (
        SELECT collectionID FROM collections WHERE key = ?
    ),
    child_collections AS (
        SELECT c.collectionID, c.collectionName, c.key, c.clientDateModified
        FROM collections c
        WHERE c.parentCollectionID = (SELECT collectionID FROM parent_collection)
    ),
    top_items AS (
        SELECT cc.collectionID, i.itemID
        FROM child_collections cc
        LEFT JOIN collectionItems ci ON ci.collectionID = cc.collectionID
        LEFT JOIN items i ON i.itemID = ci.itemID
        LEFT JOIN itemTypes it ON it.itemTypeID = i.itemTypeID
        WHERE i.itemID IS NULL OR it.typeName IN ('journalArticle', 'preprint', 'report', 'conferencePaper')
    ),
    pdf_items AS (
        SELECT DISTINCT cc.collectionID, ia.parentItemID
        FROM child_collections cc
        JOIN collectionItems ci ON ci.collectionID = cc.collectionID
        JOIN items i ON i.itemID = ci.itemID
        JOIN itemAttachments ia ON ia.parentItemID = i.itemID AND ia.contentType = 'application/pdf'
    )
    SELECT
        cc.collectionName,
        cc.key,
        cc.clientDateModified,
        COUNT(DISTINCT ti.itemID) AS total_items,
        COUNT(DISTINCT pi.parentItemID) AS pdf_items
    FROM child_collections cc
    LEFT JOIN top_items ti ON ti.collectionID = cc.collectionID
    LEFT JOIN pdf_items pi ON pi.collectionID = cc.collectionID
    GROUP BY cc.collectionID, cc.collectionName, cc.key, cc.clientDateModified
    ORDER BY cc.clientDateModified DESC, cc.collectionName ASC
    """

    with _local_connection() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, (target_key,)).fetchall()

    collections = []
    for row in rows:
        collections.append(
            {
                "name": row["collectionName"] or "",
                "key": row["key"] or "",
                "updated_at": row["clientDateModified"] or "",
                "total_items": int(row["total_items"] or 0),
                "pdf_items": int(row["pdf_items"] or 0),
            }
        )
    return collections


def _headers(content_type: str = "application/json") -> Dict[str, str]:
    if not ZOTERO_API_KEY or not ZOTERO_USER_ID:
        raise RuntimeError("ZOTERO_API_KEY or ZOTERO_USER_ID is empty in config.py")

    headers = {
        "Zotero-API-Key": ZOTERO_API_KEY,
        "Zotero-API-Version": "3",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _request_json(url: str, method: str = "GET", payload=None, headers=None):
    body = None
    request_headers = headers or _headers()
    if payload is not None:
        if isinstance(payload, bytes):
            body = payload
        else:
            body = json.dumps(payload).encode("utf-8")

    request = Request(url, data=body, headers=request_headers, method=method)
    with urlopen(request, timeout=60) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def _note_html(note: str, title: str = "") -> str:
    note_lines = []
    if title:
        note_lines.append(f"<h1>{html.escape(title)}</h1>")

    for line in note.splitlines():
        escaped = html.escape(line)
        if not escaped.strip():
            continue
        if escaped.startswith("### "):
            note_lines.append(f"<h3>{escaped[4:]}</h3>")
        elif escaped.startswith("## "):
            note_lines.append(f"<h2>{escaped[3:]}</h2>")
        elif escaped.startswith("# "):
            note_lines.append(f"<h1>{escaped[2:]}</h1>")
        else:
            note_lines.append(f"<p>{escaped}</p>")
    return "".join(note_lines)


def _month_abbr(month: int) -> str:
    return ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][month - 1]


def _collection_name_prefix(now: Optional[datetime] = None) -> str:
    current = now or datetime.now().astimezone()
    return f"{current.year}.{_month_abbr(current.month)}.{current.day:02d}"


def _request_raw(url: str, method: str = "GET", payload: Optional[bytes] = None, headers=None):
    request = Request(url, data=payload, headers=headers or _headers(), method=method)
    with urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")


def _create_collection(name: str, parent_collection: str = "") -> str:
    collection = {"name": name}
    if parent_collection:
        collection["parentCollection"] = parent_collection

    url = f"{BASE_URL}/users/{ZOTERO_USER_ID}/collections"
    response = _request_json(url, method="POST", payload=[collection])
    success = response.get("successful", {})
    if not success:
        raise RuntimeError(f"Failed to create Zotero collection: {response}")
    return next(iter(success.values()))["key"]


def _list_child_collections(parent_collection: str = "") -> list:
    if parent_collection:
        url = f"{BASE_URL}/users/{ZOTERO_USER_ID}/collections/{parent_collection}/collections"
    else:
        url = f"{BASE_URL}/users/{ZOTERO_USER_ID}/collections/top"
    raw = _request_raw(url, method="GET", headers=_headers(""))
    return json.loads(raw) if raw else []


def create_run_collection(parent_collection: str = "") -> Dict[str, str]:
    parent_key = parent_collection or ZOTERO_COLLECTION
    prefix = _collection_name_prefix()
    existing = _list_child_collections(parent_key)

    max_index = 0
    for collection in existing:
        name = collection.get("data", {}).get("name", "")
        if not name.startswith(prefix + " "):
            continue
        suffix = name.rsplit(" ", 1)[-1]
        if suffix.isdigit():
            max_index = max(max_index, int(suffix))

    run_name = f"{prefix} {max_index + 1:02d}"
    collection_key = _create_collection(run_name, parent_key)
    return {"name": run_name, "key": collection_key, "parent_key": parent_key}


def _create_item(paper: Dict, collection_key: str = "") -> str:
    target_collection = collection_key or ZOTERO_COLLECTION
    item = {
        "itemType": "journalArticle",
        "title": paper.get("title", ""),
        "abstractNote": paper.get("abstract", ""),
        "url": paper.get("url", ""),
        "collections": [target_collection] if target_collection else [],
        "archiveLocation": paper.get("source", ""),
        "accessDate": "CURRENT_TIMESTAMP",
        "date": paper.get("published", ""),
        "DOI": paper.get("doi", ""),
    }
    url = f"{BASE_URL}/users/{ZOTERO_USER_ID}/items"
    response = _request_json(url, method="POST", payload=[item])
    success = response.get("successful", {})
    if not success:
        raise RuntimeError(f"Failed to create Zotero item: {response}")
    return next(iter(success.values()))["key"]


def _create_note(parent_key: str, note: str) -> str:
    item = {
        "itemType": "note",
        "parentItem": parent_key,
        "note": _note_html(note),
    }
    url = f"{BASE_URL}/users/{ZOTERO_USER_ID}/items"
    response = _request_json(url, method="POST", payload=[item])
    success = response.get("successful", {})
    if not success:
        raise RuntimeError(f"Failed to create Zotero note: {response}")
    return next(iter(success.values()))["key"]


def _create_collection_note(collection_key: str, title: str, note: str) -> str:
    item = {
        "itemType": "note",
        "collections": [collection_key] if collection_key else [],
        "note": _note_html(note, title=title),
    }
    url = f"{BASE_URL}/users/{ZOTERO_USER_ID}/items"
    response = _request_json(url, method="POST", payload=[item])
    success = response.get("successful", {})
    if not success:
        raise RuntimeError(f"Failed to create Zotero collection note: {response}")
    return next(iter(success.values()))["key"]


def _create_attachment(parent_key: str, pdf_path: str, collection_key: str = "") -> str:
    target_collection = collection_key or ZOTERO_COLLECTION
    filename = Path(pdf_path).name
    item = {
        "itemType": "attachment",
        "parentItem": parent_key,
        "linkMode": "imported_file",
        "title": filename,
        "filename": filename,
        "contentType": "application/pdf",
        "collections": [target_collection] if target_collection else [],
    }
    url = f"{BASE_URL}/users/{ZOTERO_USER_ID}/items"
    response = _request_json(url, method="POST", payload=[item])
    success = response.get("successful", {})
    if not success:
        raise RuntimeError(f"Failed to create Zotero attachment item: {response}")
    return next(iter(success.values()))["key"]


def _upload_pdf(attachment_key: str, pdf_path: str) -> None:
    file_bytes = Path(pdf_path).read_bytes()
    filename = Path(pdf_path).name
    md5_digest = hashlib.md5(file_bytes).hexdigest()
    file_stat = os.stat(pdf_path)
    params = urlencode(
        {
            "md5": md5_digest,
            "filename": filename,
            "filesize": len(file_bytes),
            "mtime": int(file_stat.st_mtime * 1000),
        }
    ).encode("utf-8")

    auth_url = f"{BASE_URL}/users/{ZOTERO_USER_ID}/items/{attachment_key}/file"
    auth_request = Request(
        auth_url,
        data=params,
        headers=_headers("application/x-www-form-urlencoded"),
        method="POST",
    )
    with urlopen(auth_request, timeout=60) as response:
        upload_info = json.loads(response.read().decode("utf-8") or "{}")

    if upload_info.get("exists"):
        return

    upload_body = upload_info["prefix"].encode("utf-8") + file_bytes + upload_info["suffix"].encode("utf-8")
    upload_request = Request(
        upload_info["url"],
        data=upload_body,
        headers={"Content-Type": upload_info["contentType"]},
        method="POST",
    )
    with urlopen(upload_request, timeout=120):
        pass

    register_body = urlencode({"upload": upload_info["uploadKey"]}).encode("utf-8")
    register_request = Request(
        auth_url,
        data=register_body,
        headers=_headers("application/x-www-form-urlencoded"),
        method="POST",
    )
    with urlopen(register_request, timeout=60):
        pass

    time.sleep(1)


def add_to_zotero(paper: Dict, note: str, pdf_path: Optional[str], collection_key: str = "") -> Dict[str, Optional[str]]:
    item_key = _create_item(paper, collection_key=collection_key)
    note_key = None
    if (note or "").strip():
        note_key = _create_note(item_key, note)
    attachment_key = None

    if pdf_path:
        try:
            attachment_key = _create_attachment(item_key, pdf_path, collection_key=collection_key)
            _upload_pdf(attachment_key, pdf_path)
        except Exception:
            attachment_key = None

    return {
        "item_key": item_key,
        "note_key": note_key,
        "attachment_key": attachment_key,
    }


def add_note_to_existing_item(item_key: str, note: str) -> str:
    return _create_note(item_key, note)


def add_collection_note(collection_key: str, title: str, note: str) -> str:
    return _create_collection_note(collection_key, title, note)


def list_collection_items(collection_key: str) -> List[Dict]:
    return _list_collection_items_local(collection_key)
