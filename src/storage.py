#!/usr/bin/env python3
"""
状态存储模块

管理规章状态的持久化存储和变更检测。
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
    """按发布日期过滤文档，只保留最近 N 天的
    
    Args:
        documents: 文档列表
        days: 天数
    
    Returns:
        过滤后的文档列表
    """
    if days <= 0:
        return documents
    
    cutoff_date = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff_date.strftime("%Y-%m-%d")
    
    filtered = []
    for doc in documents:
        # 尝试解析发布日期
        pub_date = doc.publish_date.strip()
        if not pub_date:
            # 没有发布日期的跳过
            logger.debug(f"跳过无日期文档: {doc.doc_number} {doc.title}")
            continue
        
        # 比较日期字符串（YYYY-MM-DD 格式可以直接比较）
        if pub_date >= cutoff_str:
            filtered.append(doc)
            logger.debug(f"保留: {doc.doc_number} ({pub_date})")
        else:
            logger.debug(f"过滤: {doc.doc_number} ({pub_date}) < {cutoff_str}")
    
    return filtered


@dataclass
class StorageState:
    """存储状态"""
    last_check: str = ""
    regulations: list[dict] = field(default_factory=list)
    normatives: list[dict] = field(default_factory=list)


@dataclass
class ChangeResult:
    """变更检测结果"""
    new_regulations: list[RegulationDocument] = field(default_factory=list)
    new_normatives: list[RegulationDocument] = field(default_factory=list)
    
    @property
    def has_changes(self) -> bool:
        """是否有变更"""
        return len(self.new_regulations) > 0 or len(self.new_normatives) > 0
    
    @property
    def total_count(self) -> int:
        """变更总数"""
        return len(self.new_regulations) + len(self.new_normatives)


def atomic_write_json(file_path: str, data: dict) -> None:
    """原子写入 JSON 文件，防止数据损坏
    
    使用临时文件 + os.replace() 实现原子写入：
    - 写入临时文件
    - 同步到磁盘
    - 原子替换目标文件
    """
    dir_name = os.path.dirname(file_path) or "."
    os.makedirs(dir_name, exist_ok=True)
    
    # 写入临时文件
    fd, temp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())  # 强制同步到硬盘
        
        # 原子替换
        os.replace(temp_path, file_path)
        logger.debug(f"原子写入成功: {file_path}")
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


class Storage:
    """状态存储管理器"""

    def __init__(self, data_path: str = "data/regulations.json"):
        self.data_path = data_path
        self._state: Optional[StorageState] = None

    def load(self) -> StorageState:
        """加载状态"""
        if self._state is not None:
            return self._state
        
        if not os.path.exists(self.data_path):
            logger.info(f"状态文件不存在，创建新状态: {self.data_path}")
            self._state = StorageState()
            return self._state
        
        try:
            with open(self.data_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self._state = StorageState(
                last_check=data.get("last_check", ""),
                regulations=data.get("regulations", []),
                normatives=data.get("normatives", []),
            )
            logger.info(f"加载状态成功: {len(self._state.regulations)} 条规章, {len(self._state.normatives)} 条规范性文件")
            return self._state
            
        except json.JSONDecodeError as e:
            # JSON 解析失败，备份损坏的文件
            backup_path = f"{self.data_path}.corrupted.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            logger.error(f"JSON 解析失败: {e}，备份到 {backup_path}")
            try:
                import shutil
                shutil.copy2(self.data_path, backup_path)
            except Exception:
                pass
            self._state = StorageState()
            return self._state
            
        except Exception as e:
            logger.error(f"加载状态失败: {e}")
            self._state = StorageState()
            return self._state

    def save(self, state: StorageState) -> None:
        """保存状态"""
        data = {
            "last_check": state.last_check,
            "regulations": state.regulations,
            "normatives": state.normatives,
        }
        
        atomic_write_json(self.data_path, data)
        self._state = state
        logger.info(f"保存状态成功: {len(state.regulations)} 条规章, {len(state.normatives)} 条规范性文件")

    def detect_changes(
        self,
        current_regulations: list[RegulationDocument],
        current_normatives: list[RegulationDocument],
    ) -> ChangeResult:
        """检测变更
        
        通过 URL 判断是否为新增文档。
        
        Args:
            current_regulations: 当前规章列表
            current_normatives: 当前规范性文件列表
        
        Returns:
            变更检测结果
        """
        state = self.load()
        
        # 构建已知 URL 集合
        known_regulation_urls = {doc["url"] for doc in state.regulations}
        known_normative_urls = {doc["url"] for doc in state.normatives}
        
        # 检测新增规章
        new_regulations = [
            doc for doc in current_regulations
            if doc.url not in known_regulation_urls
        ]
        
        # 检测新增规范性文件
        new_normatives = [
            doc for doc in current_normatives
            if doc.url not in known_normative_urls
        ]
        
        result = ChangeResult(
            new_regulations=new_regulations,
            new_normatives=new_normatives,
        )
        
        if result.has_changes:
            logger.info(f"检测到变更: {len(new_regulations)} 条新规章, {len(new_normatives)} 条新规范性文件")
        else:
            logger.info("未检测到变更")
        
        return result

    def update_state(
        self,
        current_regulations: list[RegulationDocument],
        current_normatives: list[RegulationDocument],
    ) -> None:
        """更新状态
        
        将当前获取的文档列表保存为新状态。
        """
        state = StorageState(
            last_check=datetime.now().isoformat(),
            regulations=[doc.to_dict() for doc in current_regulations],
            normatives=[doc.to_dict() for doc in current_normatives],
        )
        self.save(state)
