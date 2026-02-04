#!/usr/bin/env python3
"""
State Storage Module

Manages persistent storage and change detection for document state.
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from .crawler import Document, CATEGORIES


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
    
    @property
    def has_changes(self) -> bool:
        return any(len(docs) > 0 for docs in self.new_documents.values())
    
    @property
    def total_count(self) -> int:
        return sum(len(docs) for docs in self.new_documents.values())
    
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


class Storage:
    """State storage manager"""

    def __init__(self, data_path: str = "data/regulations.json"):
        self.data_path = data_path
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
            
            self._state = StorageState(
                last_check=data.get("last_check", ""),
                documents=data.get("documents", {}),
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
        total_new = 0
        
        for cat_id, docs in current_documents.items():
            known_urls = set()
            if cat_id in state.documents:
                known_urls = {doc["url"] for doc in state.documents[cat_id]}
            
            new_docs = [doc for doc in docs if doc.url not in known_urls]
            
            if new_docs:
                new_documents[cat_id] = new_docs
                total_new += len(new_docs)
                cat_name = CATEGORIES.get(cat_id, cat_id)
                logger.info(f"New in {cat_name}: {len(new_docs)} documents")
        
        result = ChangeResult(new_documents=new_documents)
        
        if result.has_changes:
            logger.info(f"Total changes detected: {total_new} new documents")
        else:
            logger.info("No changes detected")
        
        return result

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
