#!/usr/bin/env python3
"""
State Storage Module

Manages persistent storage and change detection for regulation state.
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from .crawler import RegulationDocument


def filter_by_days(documents: list[RegulationDocument], days: int) -> list[RegulationDocument]:
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
    regulations: list[dict] = field(default_factory=list)
    normatives: list[dict] = field(default_factory=list)
    standards: list[dict] = field(default_factory=list)


@dataclass
class ChangeResult:
    """Change detection result"""
    new_regulations: list[RegulationDocument] = field(default_factory=list)
    new_normatives: list[RegulationDocument] = field(default_factory=list)
    new_standards: list[RegulationDocument] = field(default_factory=list)
    
    @property
    def has_changes(self) -> bool:
        return len(self.new_regulations) > 0 or len(self.new_normatives) > 0 or len(self.new_standards) > 0
    
    @property
    def total_count(self) -> int:
        return len(self.new_regulations) + len(self.new_normatives) + len(self.new_standards)


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
                regulations=data.get("regulations", []),
                normatives=data.get("normatives", []),
                standards=data.get("standards", []),
            )
            logger.info(f"State loaded: {len(self._state.regulations)} regulations, {len(self._state.normatives)} normatives, {len(self._state.standards)} standards")
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
            "regulations": state.regulations,
            "normatives": state.normatives,
            "standards": state.standards,
        }
        
        atomic_write_json(self.data_path, data)
        self._state = state
        logger.info(f"State saved: {len(state.regulations)} regulations, {len(state.normatives)} normatives, {len(state.standards)} standards")

    def detect_changes(
        self,
        current_regulations: list[RegulationDocument],
        current_normatives: list[RegulationDocument],
        current_standards: list[RegulationDocument],
    ) -> ChangeResult:
        """Detect changes by URL"""
        state = self.load()
        
        known_regulation_urls = {doc["url"] for doc in state.regulations}
        known_normative_urls = {doc["url"] for doc in state.normatives}
        known_standard_urls = {doc["url"] for doc in state.standards}
        
        new_regulations = [
            doc for doc in current_regulations
            if doc.url not in known_regulation_urls
        ]
        
        new_normatives = [
            doc for doc in current_normatives
            if doc.url not in known_normative_urls
        ]
        
        new_standards = [
            doc for doc in current_standards
            if doc.url not in known_standard_urls
        ]
        
        result = ChangeResult(
            new_regulations=new_regulations,
            new_normatives=new_normatives,
            new_standards=new_standards,
        )
        
        if result.has_changes:
            logger.info(f"Changes detected: {len(new_regulations)} regulations, {len(new_normatives)} normatives, {len(new_standards)} standards")
        else:
            logger.info("No changes detected")
        
        return result

    def update_state(
        self,
        current_regulations: list[RegulationDocument],
        current_normatives: list[RegulationDocument],
        current_standards: list[RegulationDocument],
    ) -> None:
        """Update state with current document list"""
        state = StorageState(
            last_check=datetime.now().isoformat(),
            regulations=[doc.to_dict() for doc in current_regulations],
            normatives=[doc.to_dict() for doc in current_normatives],
            standards=[doc.to_dict() for doc in current_standards],
        )
        self.save(state)
