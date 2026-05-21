# -*- coding: utf-8 -*-
"""
===================================
用户自选股订阅数据访问层
===================================

职责：
1. 封装 UserSubscription 表的数据库操作
2. 提供订阅 CRUD 接口
"""

import logging
from datetime import datetime
from typing import Optional, List

from sqlalchemy import and_

from src.storage import DatabaseManager, UserSubscription

logger = logging.getLogger(__name__)


class SubscriptionRepository:
    """用户自选股订阅数据访问层"""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def upsert(
        self,
        platform: str,
        user_id: str,
        chat_id: str,
        chat_type: str,
        stock_codes: str,
        push_hour: int = 18,
        push_minute: int = 0,
        report_type: str = "simple",
        message_id: Optional[str] = None,
    ) -> UserSubscription:
        """
        插入或更新订阅（按 platform + user_id + chat_id 唯一）

        Returns:
            保存后的 UserSubscription 对象
        """
        session = self.db.get_session()
        try:
            existing = (
                session.query(UserSubscription)
                .filter(
                    UserSubscription.platform == platform,
                    UserSubscription.user_id == user_id,
                    UserSubscription.chat_id == chat_id,
                )
                .one_or_none()
            )

            if existing:
                existing.stock_codes = stock_codes
                existing.push_hour = push_hour
                existing.push_minute = push_minute
                existing.report_type = report_type
                existing.enabled = True
                existing.updated_at = datetime.now()
                if message_id is not None:
                    existing.message_id = message_id
                session.commit()
                session.refresh(existing)
                return existing
            else:
                sub = UserSubscription(
                    platform=platform,
                    user_id=user_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    message_id=message_id,
                    stock_codes=stock_codes,
                    push_hour=push_hour,
                    push_minute=push_minute,
                    report_type=report_type,
                    enabled=True,
                )
                session.add(sub)
                session.commit()
                session.refresh(sub)
                return sub
        except Exception as e:
            session.rollback()
            logger.error(f"[SubscriptionRepo] upsert 失败: {e}")
            raise
        finally:
            session.close()

    def get_active(self, user_id: str, chat_id: str) -> Optional[UserSubscription]:
        """获取某用户在某聊天的活跃订阅"""
        session = self.db.get_session()
        try:
            return (
                session.query(UserSubscription)
                .filter(
                    UserSubscription.user_id == user_id,
                    UserSubscription.chat_id == chat_id,
                    UserSubscription.enabled == True,
                )
                .one_or_none()
            )
        finally:
            session.close()

    def list_due(self, hour: int, minute: int) -> List[UserSubscription]:
        """查询所有 enabled=True 且 push 时间匹配的订阅"""
        session = self.db.get_session()
        try:
            return (
                session.query(UserSubscription)
                .filter(
                    UserSubscription.enabled == True,
                    UserSubscription.push_hour == hour,
                    UserSubscription.push_minute == minute,
                )
                .all()
            )
        finally:
            session.close()

    def disable(self, user_id: str, chat_id: str) -> bool:
        """停用订阅"""
        session = self.db.get_session()
        try:
            sub = (
                session.query(UserSubscription)
                .filter(
                    UserSubscription.user_id == user_id,
                    UserSubscription.chat_id == chat_id,
                )
                .one_or_none()
            )
            if not sub:
                return False
            sub.enabled = False
            sub.updated_at = datetime.now()
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error(f"[SubscriptionRepo] disable 失败: {e}")
            return False
        finally:
            session.close()

    def list_by_user(self, user_id: str) -> List[UserSubscription]:
        """查询某用户所有订阅"""
        session = self.db.get_session()
        try:
            return (
                session.query(UserSubscription)
                .filter(UserSubscription.user_id == user_id)
                .order_by(UserSubscription.updated_at.desc())
                .all()
            )
        finally:
            session.close()
