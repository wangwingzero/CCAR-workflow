#!/usr/bin/env python3
"""
State Storage Module

Manages persistent storage and change detection for document state.
"""

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from .crawler import CATEGORIES, Document, get_legacy_doc_type


LEGACY_STATE_KEYS = {
    "13": "regulations",
    "14": "normatives",
    "15": "standards",
}

JS_EXPORT_CONFIG = {
    "13": {
        "filename": "regulation.js",
        "export_name": "regulationData",
        "doc_type": "CCAR规章",
    },
    "14": {
        "filename": "normative.js",
        "export_name": "normativeData",
        "doc_type": "规范性文件",
    },
    "15": {
        "filename": "specification.js",
        "export_name": "standardData",
        "doc_type": "标准规范",
    },
}

TRACKED_CHANGE_FIELDS = (
    "title",
    "doc_number",
    "validity",
    "office_unit",
    "sign_date",
    "publish_date",
)

LEGACY_STATIC_HOST = "https://ccar.hudawang.cn/"
STATIC_HOST = "https://flighttoolbox.hudawang.cn/"


def _normalize_pdf_url(pdf_url: str) -> str:
    """Normalize legacy mirror URLs to the current static host."""
    value = (pdf_url or "").strip()
    if value.startswith(LEGACY_STATIC_HOST):
        return STATIC_HOST + value[len(LEGACY_STATIC_HOST):]
    return value



def _compact(value: str) -> str:
    """Remove all whitespace for filename matching."""
    return re.sub(r"\s+", "", value or "")


def build_pdf_url_fallback(data_path: str) -> tuple[dict[str, str], dict[str, str]]:
    """Reconstruct historical PDF URLs from the committed data indexes.

    CI runners are ephemeral (downloads/ is always empty), so attachment URLs
    can only be recovered from data/downloads.json -> data/r2_uploads.json.
    R2 filenames follow the deterministic ``{validity!}{doc_number}{title}.pdf``
    convention, so rows whose download record predates the R2 index can still
    be matched by normalized doc_number+title / title.

    Returns:
        (by_url, by_filename): {caac page url: r2 url} and
        {normalized "{doc_number}{title}" or "{title}": r2 url}
    """
    data_dir = Path(data_path).parent

    r2_index: dict[str, dict] = {}
    r2_index_path = data_dir / "r2_uploads.json"
    if r2_index_path.exists():
        try:
            with open(r2_index_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                r2_index = payload.get("records", {})
        except Exception:
            r2_index = {}

    by_url: dict[str, str] = {}
    download_index_path = data_dir / "downloads.json"
    if download_index_path.exists():
        try:
            with open(download_index_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            download_records = payload.get("records", {}) if isinstance(payload, dict) else {}
        except Exception:
            download_records = {}
        for url, record in download_records.items():
            rel_path = str(record.get("relative_path", "")).strip()
            if rel_path.lower().endswith(".pdf"):
                cached = r2_index.get(rel_path)
                if cached and cached.get("r2_url"):
                    by_url[url] = _normalize_pdf_url(str(cached["r2_url"]))

    by_filename: dict[str, str] = {}
    for rel_path, record in r2_index.items():
        r2_url = record.get("r2_url") if isinstance(record, dict) else None
        if not r2_url:
            continue
        stem = rel_path.rsplit("/", 1)[-1]
        if not stem.lower().endswith(".pdf"):
            continue
        stem = stem[:-4]
        stem = re.sub(r"^(失效|废止|历史版本|作废)!", "", stem)
        stem = re.sub(r"^\[(失效|废止|历史版本|作废)\]", "", stem)
        normalized = _compact(stem)
        if normalized:
            by_filename.setdefault(normalized, _normalize_pdf_url(str(r2_url)))

    return by_url, by_filename


def filter_by_days(documents: list[Document], days: int) -> list[Document]:
    """Filter documents by publish date, keep only last N days
    
    Args:
        documents: Document list
        days: Number of days
    
    Returns:
        Filtered document list
    """
    if days <= 0:
        return documents
    
    cutoff_date = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff_date.strftime("%Y-%m-%d")
    
    filtered = []
    for doc in documents:
        pub_date = doc.publish_date.strip()
        if not pub_date:
            logger.debug(f"Skipping document without date: {doc.doc_number} {doc.title}")
            continue
        
        if pub_date >= cutoff_str:
            filtered.append(doc)
            logger.debug(f"Keeping: {doc.doc_number} ({pub_date})")
        else:
            logger.debug(f"Filtering: {doc.doc_number} ({pub_date}) < {cutoff_str}")
    
    return filtered


@dataclass
class StorageState:
    """Storage state"""
    last_check: str = ""
    documents: dict = field(default_factory=dict)


@dataclass
class ChangeResult:
    """Change detection result"""
    new_documents: dict = field(default_factory=dict)
    updated_documents: dict = field(default_factory=dict)
    reposted_documents: dict = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        return self.new_count > 0 or self.updated_count > 0

    @property
    def has_new_documents(self) -> bool:
        return self.new_count > 0

    @property
    def has_updated_documents(self) -> bool:
        return self.updated_count > 0

    @property
    def new_count(self) -> int:
        return sum(len(docs) for docs in self.new_documents.values())

    @property
    def updated_count(self) -> int:
        return sum(len(docs) for docs in self.updated_documents.values())
    
    @property
    def reposted_count(self) -> int:
        return sum(len(docs) for docs in self.reposted_documents.values())

    @property
    def total_count(self) -> int:
        return self.new_count + self.updated_count
    
    def get_all_documents(self) -> list:
        """Get all new documents as a flat list"""
        result = []
        for docs in self.new_documents.values():
            result.extend(docs)
        return result
    
    def get_documents_by_category(self) -> dict:
        """Get new documents grouped by category name"""
        result = {}
        for cat_id, docs in self.new_documents.items():
            if docs:
                cat_name = CATEGORIES.get(cat_id, f"未知分类({cat_id})")
                result[cat_name] = docs
        return result


def atomic_write_json(file_path: str, data: dict) -> None:
    """Atomic JSON file write to prevent data corruption"""
    dir_name = os.path.dirname(file_path) or "."
    os.makedirs(dir_name, exist_ok=True)
    
    fd, temp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        
        os.replace(temp_path, file_path)
        logger.debug(f"Atomic write successful: {file_path}")
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def _normalize_documents(raw_documents: dict[Any, Any]) -> dict[str, list[dict]]:
    """Normalize loaded state documents"""
    normalized = {}
    for cat_id, docs in raw_documents.items():
        if isinstance(docs, list):
            normalized[str(cat_id)] = docs
    return normalized


def _load_legacy_documents(data: dict) -> dict[str, list[dict]]:
    """Load legacy state format to category-id keyed documents"""
    documents = {}
    for cat_id, state_key in LEGACY_STATE_KEYS.items():
        docs = data.get(state_key)
        if isinstance(docs, list):
            documents[cat_id] = docs

    for cat_id in CATEGORIES:
        docs = data.get(cat_id)
        if isinstance(docs, list):
            documents[cat_id] = docs

    return documents


def _build_legacy_record(doc: dict, cat_id: str) -> dict:
    """Build legacy record format for backward compatibility"""
    return {
        "title": doc.get("title", ""),
        "url": doc.get("url", ""),
        "validity": doc.get("validity", ""),
        "doc_number": doc.get("doc_number", ""),
        "office_unit": doc.get("office_unit", ""),
        "doc_type": get_legacy_doc_type(cat_id),
        "sign_date": doc.get("sign_date", ""),
        "publish_date": doc.get("publish_date", ""),
        "pdf_url": _normalize_pdf_url(doc.get("pdf_url", "")),
    }


def _doc_field_value(doc_data: Any, field_name: str) -> str:
    """Read a normalized string field from dict or object"""
    if isinstance(doc_data, dict):
        value = doc_data.get(field_name, "")
    else:
        value = getattr(doc_data, field_name, "")
    return str(value or "").strip()


def _doc_signature(doc_data: Any) -> tuple[str, ...]:
    """Build a comparable signature for change detection"""
    return tuple(_doc_field_value(doc_data, name) for name in TRACKED_CHANGE_FIELDS)


def _is_repost(doc: Any, known_docs: list) -> bool:
    """True if a new-URL doc is a CAAC re-post of an already-known document.

    CAAC sometimes re-publishes the same document under a new page URL, and
    the re-posted copy may carry a different (or typo'd) doc_number — e.g.
    MH/T 2021-2026 re-posted as MH/T 1021-2026. Match by identical
    doc_number, or by identical title plus a shared date, so re-posts are
    suppressed instead of notified again. A revised standard shares neither
    its doc_number nor its publish_date with the superseded one, so it still
    surfaces as new.
    """
    doc_number = _compact(_doc_field_value(doc, "doc_number"))
    doc_title = _compact(_doc_field_value(doc, "title"))
    publish_date = _doc_field_value(doc, "publish_date")
    sign_date = _doc_field_value(doc, "sign_date")

    for known in known_docs:
        if doc_number and doc_number == _compact(_doc_field_value(known, "doc_number")):
            return True
        if doc_title and doc_title == _compact(_doc_field_value(known, "title")):
            if publish_date and publish_date == _doc_field_value(known, "publish_date"):
                return True
            if sign_date and sign_date == _doc_field_value(known, "sign_date"):
                return True
    return False


def _format_js_date(date_str: str) -> str:
    """Format date to JS output style (YYYY年MM月DD日)"""
    value = (date_str or "").strip()
    if not value:
        return ""

    iso_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
    if iso_match:
        year, month, day = iso_match.groups()
        return f"{year}年{month}月{day}日"

    cn_match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", value)
    if cn_match:
        year, month, day = cn_match.groups()
        return f"{year}年{month.zfill(2)}月{day.zfill(2)}日"

    return value


def _read_js_data(file_path: Path) -> list[dict]:
    """Read JS data array from file"""
    if not file_path.exists():
        return []

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to read JS file {file_path}: {e}")
        return []

    marker_match = re.search(r"var\s+[A-Za-z_$][\w$]*\s*=\s*\[", content)
    if not marker_match:
        logger.warning(f"Invalid JS format, missing 'var <name> = [': {file_path}")
        return []

    list_start = marker_match.end() - 1
    list_end = content.rfind("];")
    if list_start < 0 or list_end < list_start:
        logger.warning(f"Invalid JS format, missing array section: {file_path}")
        return []

    try:
        parsed = json.loads(content[list_start:list_end + 1])
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse JS data array from {file_path}: {e}")
        return []

    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _write_js_data(file_path: Path, records: list[dict], export_name: str) -> None:
    """Write JS module with stable format"""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(records, ensure_ascii=False, indent=2)
    content = (
        f"var data = {serialized};\n\n"
        f"module.exports = {{\n"
        f"  {export_name}: data\n"
        f"}};\n"
    )
    file_path.write_text(content, encoding="utf-8")


def _write_json_data(file_path: Path, records: list[dict]) -> None:
    """Write pure JSON array for R2 hot-update endpoint"""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_regulation_record(doc: Document, doc_type: str, pdf_url: str = "") -> dict:
    """Build regulation JS record"""
    record = {
        "title": doc.title or "",
        "url": doc.url or "",
        "doc_type": doc_type,
        "validity": doc.validity or "",
        "doc_number": doc.doc_number or "",
        "office_unit": doc.office_unit or "",
    }
    if pdf_url:
        record["pdf_url"] = pdf_url
    return record


def _build_normative_record(doc: Document, doc_type: str, cached_file_number: str, pdf_url: str = "") -> dict:
    """Build normative JS record"""
    record = {
        "title": doc.title or "",
        "url": doc.url or "",
        "doc_type": doc_type,
        "validity": doc.validity or "",
        "sign_date": _format_js_date(doc.sign_date),
        "publish_date": _format_js_date(doc.publish_date),
        "doc_number": doc.doc_number or "",
    }

    office_unit = (doc.office_unit or "").strip()
    if office_unit:
        record["office_unit"] = office_unit

    if cached_file_number:
        file_number = cached_file_number
    else:
        file_number = f"文号：{doc.doc_number}" if doc.doc_number else ""
    record["file_number"] = file_number

    if pdf_url:
        record["pdf_url"] = pdf_url
    return record


def _build_standard_record(doc: Document, doc_type: str, pdf_url: str = "") -> dict:
    """Build standard JS record"""
    record = {
        "title": doc.title or "",
        "url": doc.url or "",
        "doc_type": doc_type,
        "validity": doc.validity or "",
        "publish_date": _format_js_date(doc.publish_date),
        "doc_number": doc.doc_number or "",
        "office_unit": doc.office_unit or "",
    }
    if pdf_url:
        record["pdf_url"] = pdf_url
    return record


class Storage:
    """State storage manager"""

    def __init__(self, data_path: str = "data/regulations.json"):
        self.data_path = data_path
        self.download_index_path = str(Path(data_path).with_name("downloads.json"))
        self._state: Optional[StorageState] = None

    def load(self) -> StorageState:
        """Load state"""
        if self._state is not None:
            return self._state
        
        if not os.path.exists(self.data_path):
            logger.info(f"State file not found, creating new: {self.data_path}")
            self._state = StorageState()
            return self._state
        
        try:
            with open(self.data_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            loaded_documents = data.get("documents", {})
            if not isinstance(loaded_documents, dict) or not loaded_documents:
                loaded_documents = _load_legacy_documents(data)

            self._state = StorageState(
                last_check=data.get("last_check", ""),
                documents=_normalize_documents(loaded_documents),
            )
            
            total_docs = sum(len(docs) for docs in self._state.documents.values())
            logger.info(f"State loaded: {total_docs} documents in {len(self._state.documents)} categories")
            return self._state
            
        except json.JSONDecodeError as e:
            backup_path = f"{self.data_path}.corrupted.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            logger.error(f"JSON parse failed: {e}, backing up to {backup_path}")
            try:
                import shutil
                shutil.copy2(self.data_path, backup_path)
            except Exception:
                pass
            self._state = StorageState()
            return self._state
            
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            self._state = StorageState()
            return self._state

    def save(self, state: StorageState) -> None:
        """Save state"""
        data = {
            "last_check": state.last_check,
            "documents": state.documents,
        }

        for cat_id, state_key in LEGACY_STATE_KEYS.items():
            docs = state.documents.get(cat_id, [])
            if not isinstance(docs, list):
                docs = []
            legacy_docs = [_build_legacy_record(doc, cat_id) for doc in docs if isinstance(doc, dict)]
            data[state_key] = legacy_docs
        
        atomic_write_json(self.data_path, data)
        self._state = state
        
        total_docs = sum(len(docs) for docs in state.documents.values())
        logger.info(f"State saved: {total_docs} documents in {len(state.documents)} categories")

    def detect_changes(self, current_documents: dict) -> ChangeResult:
        """Detect changes by URL
        
        Args:
            current_documents: Dictionary mapping category_id to list of documents
        
        Returns:
            ChangeResult with new documents
        """
        state = self.load()
        
        new_documents = {}
        updated_documents = {}
        reposted_documents = {}

        for cat_id, docs in current_documents.items():
            known_by_url = {}
            for known_doc in state.documents.get(cat_id, []):
                known_url = _doc_field_value(known_doc, "url")
                if known_url:
                    known_by_url[known_url] = known_doc

            new_docs = []
            changed_docs = []
            reposted_docs = []
            known_docs = state.documents.get(cat_id, [])

            for doc in docs:
                known_doc = known_by_url.get(doc.url)
                if known_doc is None:
                    if _is_repost(doc, known_docs):
                        reposted_docs.append(doc)
                    else:
                        new_docs.append(doc)
                elif _doc_signature(known_doc) != _doc_signature(doc):
                    changed_docs.append(doc)

            if new_docs:
                new_documents[cat_id] = new_docs
                cat_name = CATEGORIES.get(cat_id, cat_id)
                logger.info(f"New in {cat_name}: {len(new_docs)} documents")

            if changed_docs:
                updated_documents[cat_id] = changed_docs
                cat_name = CATEGORIES.get(cat_id, cat_id)
                logger.info(f"Updated in {cat_name}: {len(changed_docs)} documents")

            if reposted_docs:
                reposted_documents[cat_id] = reposted_docs
                cat_name = CATEGORIES.get(cat_id, cat_id)
                logger.info(
                    f"Reposted in {cat_name}: {len(reposted_docs)} documents "
                    f"(same doc under new URL, ignored)"
                )
        
        result = ChangeResult(
            new_documents=new_documents,
            updated_documents=updated_documents,
            reposted_documents=reposted_documents,
        )
        
        if result.has_changes:
            logger.info(
                f"Total changes detected: new={result.new_count}, "
                f"updated={result.updated_count}, total={result.total_count}"
            )
        else:
            logger.info("No changes detected")
        
        return result

    def load_download_index(self) -> dict[str, dict]:
        """Load URL -> local file path index for downloaded files"""
        if not os.path.exists(self.download_index_path):
            return {}

        try:
            with open(self.download_index_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            raw_records = data.get("records", data) if isinstance(data, dict) else {}
            if not isinstance(raw_records, dict):
                return {}

            records: dict[str, dict] = {}
            for url, record in raw_records.items():
                if not isinstance(url, str) or not isinstance(record, dict):
                    continue
                rel_path = str(record.get("relative_path", "")).strip()
                if not rel_path:
                    continue
                records[url] = {
                    "relative_path": rel_path,
                    "updated_at": str(record.get("updated_at", "")).strip(),
                }
            return records
        except Exception as e:
            logger.warning(f"Failed to load download index {self.download_index_path}: {e}")
            return {}

    def save_download_index(self, records: dict[str, dict]) -> None:
        """Save URL -> local file path index"""
        payload = {
            "last_update": datetime.now().isoformat(),
            "records": records,
        }
        atomic_write_json(self.download_index_path, payload)
        logger.info(f"Download index saved: {len(records)} entries")

    def purge_documents(self, urls: set[str], js_dir: str = "JS") -> dict[str, int]:
        """Remove documents by URL from state, download/R2 indexes and JS exports.

        Used to clean up rows that should never have been recorded (e.g. raw
        attachment hits returned by the search API). Objects already uploaded
        to R2 are only dropped from the index, not deleted from the bucket.

        Returns per-store removal counts.
        """
        removal_counts = {"state": 0, "downloads": 0, "r2_index": 0, "js": 0}
        if not urls:
            return removal_counts

        state = self.load()
        cleaned_documents: dict[str, list] = {}
        for cat_id, rows in state.documents.items():
            kept_rows = [
                row for row in rows
                if not (isinstance(row, dict) and str(row.get("url", "")).strip() in urls)
            ]
            removal_counts["state"] += len(rows) - len(kept_rows)
            cleaned_documents[cat_id] = kept_rows
        self.save(StorageState(last_check=state.last_check, documents=cleaned_documents))

        download_index = self.load_download_index()
        purged_relative_paths = {
            record["relative_path"] for url, record in download_index.items() if url in urls
        }
        remaining_download_index = {
            url: record for url, record in download_index.items() if url not in urls
        }
        removal_counts["downloads"] = len(download_index) - len(remaining_download_index)
        if removal_counts["downloads"]:
            self.save_download_index(remaining_download_index)

        # Only drop R2 entries no surviving download record still points to.
        still_referenced_paths = {record["relative_path"] for record in remaining_download_index.values()}
        orphaned_relative_paths = purged_relative_paths - still_referenced_paths
        r2_index_path = Path(self.data_path).parent / "r2_uploads.json"
        if orphaned_relative_paths and r2_index_path.exists():
            with open(r2_index_path, "r", encoding="utf-8") as f:
                r2_payload = json.load(f)
            r2_records = r2_payload.get("records", {})
            for relative_path in orphaned_relative_paths:
                if r2_records.pop(relative_path, None) is not None:
                    removal_counts["r2_index"] += 1
            if removal_counts["r2_index"]:
                r2_payload["last_update"] = datetime.now().isoformat()
                atomic_write_json(str(r2_index_path), r2_payload)

        js_root = Path(js_dir)
        for config in JS_EXPORT_CONFIG.values():
            js_path = js_root / config["filename"]
            existing_rows = _read_js_data(js_path)
            kept_rows = [row for row in existing_rows if str(row.get("url", "")).strip() not in urls]
            if len(kept_rows) == len(existing_rows):
                continue
            removal_counts["js"] += len(existing_rows) - len(kept_rows)
            _write_js_data(js_path, kept_rows, config["export_name"])
            _write_json_data(js_path.with_suffix(".json"), kept_rows)

        logger.info(
            "Purged documents: "
            + ", ".join(f"{store}={count}" for store, count in removal_counts.items())
        )
        return removal_counts

    def update_state(self, current_documents: dict) -> None:
        """Update state with current document list.

        Merge semantics: a category that came back empty this run (a transient
        crawl/WAF failure) keeps its previous state instead of being wiped.
        Overwriting it with an empty list would make every document in that
        category re-surface as "new" on the next successful run (notification
        flood + full re-download). Non-empty partial fetches also merge by URL:
        current rows refresh metadata, historical rows not seen this run stay
        recorded so a transient truncation cannot turn them into future "new"
        documents. Mirrors the protection in ``sync_js_files``.
        """
        previous = self.load().documents
        documents_dict: dict[str, list[dict]] = {}
        for cat_id, docs in current_documents.items():
            if docs:
                current_rows = [doc.to_dict() for doc in docs]
                # List pages don't expose attachment URLs; carry over the
                # previously known pdf_url for the same page so a metadata
                # refresh cannot silently wipe it.
                previous_pdf_url_by_url = {}
                for prev_row in previous.get(cat_id, []):
                    if not isinstance(prev_row, dict):
                        continue
                    prev_url = str(prev_row.get("url", "")).strip()
                    prev_pdf_url = _normalize_pdf_url(str(prev_row.get("pdf_url", "")).strip())
                    if prev_url and prev_pdf_url:
                        previous_pdf_url_by_url[prev_url] = prev_pdf_url
                for row in current_rows:
                    if not _normalize_pdf_url(str(row.get("pdf_url", "")).strip()):
                        row_url = str(row.get("url", "")).strip()
                        if row_url in previous_pdf_url_by_url:
                            row["pdf_url"] = previous_pdf_url_by_url[row_url]
                seen_urls = {
                    str(row.get("url", "")).strip()
                    for row in current_rows
                    if isinstance(row, dict) and row.get("url")
                }
                documents_dict[cat_id] = current_rows + [
                    row for row in previous.get(cat_id, [])
                    if not isinstance(row, dict)
                    or not str(row.get("url", "")).strip()
                    or str(row.get("url", "")).strip() not in seen_urls
                ]
            elif previous.get(cat_id):
                logger.warning(
                    f"Category {cat_id} crawled empty; preserving "
                    f"{len(previous[cat_id])} previous entries to avoid false 'new' detection"
                )
                documents_dict[cat_id] = previous[cat_id]
        # Categories not crawled at all this run: keep their previous state.
        for cat_id, prev_docs in previous.items():
            documents_dict.setdefault(cat_id, prev_docs)
        
        state = StorageState(
            last_check=datetime.now().isoformat(),
            documents=documents_dict,
        )
        self.save(state)

    def sync_js_files(self, current_documents: dict[str, list[Document]], js_dir: str = "JS", r2_url_map: dict[str, str] | None = None) -> dict[str, int]:
        """Sync JS data files for 13/14/15 categories

        Args:
            current_documents: Category-id keyed document lists
            js_dir: Output directory for JS files
            r2_url_map: Optional mapping from CAAC page URL to R2 PDF URL
        """
        js_root = Path(js_dir)
        summary = {}
        url_map = r2_url_map or {}
        fallback_urls, fallback_filenames = build_pdf_url_fallback(self.data_path)

        def resolve_pdf_url(doc: Document) -> str:
            """Resolve pdf_url: this run's R2 map > committed indexes > existing JS."""
            url = url_map.get(doc.url) or fallback_urls.get(doc.url)
            if url:
                return url
            doc_number = _compact(doc.doc_number)
            title = _compact(doc.title)
            candidate = (doc_number + title) if doc_number else title
            if candidate and candidate in fallback_filenames:
                return fallback_filenames[candidate]
            return existing_pdf_url_by_url.get(doc.url, "")

        normative_cfg = JS_EXPORT_CONFIG["14"]
        normative_path = js_root / normative_cfg["filename"]
        existing_normative_data = _read_js_data(normative_path)
        existing_file_number_by_url = {}
        for row in existing_normative_data:
            url = str(row.get("url", "")).strip()
            file_number = str(row.get("file_number", "")).strip()
            if url and file_number:
                existing_file_number_by_url[url] = file_number

        for cat_id, config in JS_EXPORT_CONFIG.items():
            file_path = js_root / config["filename"]
            export_name = config["export_name"]
            doc_type = config["doc_type"]
            docs = current_documents.get(cat_id)
            cat_name = CATEGORIES.get(cat_id, cat_id)
            existing_rows = _read_js_data(file_path)

            # Build lookup for existing pdf_url to preserve during merge
            existing_pdf_url_by_url = {}
            for row in existing_rows:
                row_url = str(row.get("url", "")).strip()
                row_pdf_url = _normalize_pdf_url(str(row.get("pdf_url", "")).strip())
                if row_url and row_pdf_url:
                    existing_pdf_url_by_url[row_url] = row_pdf_url

            if not docs:
                if existing_rows:
                    reason = "未抓取该分类" if docs is None else "该分类本次抓取为空"
                    logger.warning(f"{cat_name}: {reason}，保留现有 JS 文件 {file_path}")
                    summary[file_path.name] = len(existing_rows)
                    continue

                logger.warning(f"{cat_name}: 无可用数据，写入空 JS 文件 {file_path}")
                _write_js_data(file_path, [], export_name)
                summary[file_path.name] = 0
                continue

            if cat_id == "13":
                records = [
                    _build_regulation_record(
                        doc, doc_type,
                        pdf_url=_normalize_pdf_url(resolve_pdf_url(doc))
                    )
                    for doc in docs
                ]
            elif cat_id == "14":
                records = [
                    _build_normative_record(
                        doc, doc_type,
                        existing_file_number_by_url.get(doc.url, ""),
                        pdf_url=_normalize_pdf_url(resolve_pdf_url(doc))
                    )
                    for doc in docs
                ]
            else:
                records = [
                    _build_standard_record(
                        doc, doc_type,
                        pdf_url=_normalize_pdf_url(resolve_pdf_url(doc))
                    )
                    for doc in docs
                ]

            # Keep existing history to avoid truncating JS when perpage is small.
            seen_urls = {row.get("url", "") for row in records if row.get("url")}
            merged_records = records + [
                row for row in existing_rows
                if not row.get("url") or row.get("url") not in seen_urls
            ]

            _write_js_data(file_path, merged_records, export_name)
            json_path = file_path.with_suffix(".json")
            _write_json_data(json_path, merged_records)
            summary[file_path.name] = len(merged_records)
            logger.info(
                f"JS updated: {file_path} "
                f"(new={len(records)}, kept={len(merged_records) - len(records)}, total={len(merged_records)})"
            )

        return summary
