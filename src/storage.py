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
        "pdf_url": doc.get("pdf_url", ""),
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

    marker_pos = content.find("var data")
    if marker_pos < 0:
        logger.warning(f"Invalid JS format, missing 'var data': {file_path}")
        return []

    list_start = content.find("[", marker_pos)
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

        for cat_id, docs in current_documents.items():
            known_by_url = {}
            for known_doc in state.documents.get(cat_id, []):
                known_url = _doc_field_value(known_doc, "url")
                if known_url:
                    known_by_url[known_url] = known_doc

            new_docs = []
            changed_docs = []

            for doc in docs:
                known_doc = known_by_url.get(doc.url)
                if known_doc is None:
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
        
        result = ChangeResult(
            new_documents=new_documents,
            updated_documents=updated_documents,
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

    def update_state(self, current_documents: dict) -> None:
        """Update state with current document list"""
        documents_dict = {}
        for cat_id, docs in current_documents.items():
            documents_dict[cat_id] = [doc.to_dict() for doc in docs]
        
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
                row_pdf_url = str(row.get("pdf_url", "")).strip()
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
                        pdf_url=url_map.get(doc.url, existing_pdf_url_by_url.get(doc.url, ""))
                    )
                    for doc in docs
                ]
            elif cat_id == "14":
                records = [
                    _build_normative_record(
                        doc, doc_type,
                        existing_file_number_by_url.get(doc.url, ""),
                        pdf_url=url_map.get(doc.url, existing_pdf_url_by_url.get(doc.url, ""))
                    )
                    for doc in docs
                ]
            else:
                records = [
                    _build_standard_record(
                        doc, doc_type,
                        pdf_url=url_map.get(doc.url, existing_pdf_url_by_url.get(doc.url, ""))
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
            summary[file_path.name] = len(merged_records)
            logger.info(
                f"JS updated: {file_path} "
                f"(new={len(records)}, kept={len(merged_records) - len(records)}, total={len(merged_records)})"
            )

        return summary
