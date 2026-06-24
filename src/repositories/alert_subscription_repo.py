# -*- coding: utf-8 -*-
"""
信号监控订阅数据访问层
"""

import logging
from datetime import datetime
from typing import Optional, List

from sqlalchemy import and_

from src.storage import DatabaseManager, AlertSubscription

logger = logging.getLogger(__name__)


class AlertSubscriptionRepo:
    """信号监控订阅数据访问层"""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def upsert(
        self,
        platform: str,
        user_id: str,
        chat_id: str,
        chat_type: str,
        stock_codes: str,
        message_id: Optional[str] = None,
    ) -> AlertSubscription:
        session = self.db.get_session()
        try:
            sub = (
                session.query(AlertSubscription)
                .filter(
                    AlertSubscription.platform == platform,
                    AlertSubscription.user_id == user_id,
                    AlertSubscription.chat_id == chat_id,
                )
                .one_or_none()
            )
            if sub:
                existing = set(c.strip() for c in (sub.stock_codes or "").split(",") if c.strip())
                new_codes = [c.strip() for c in stock_codes.split(",") if c.strip()]
                merged = list(dict.fromkeys(
                    [c for c in (sub.stock_codes or "").split(",") if c.strip()] +
                    [c for c in new_codes if c not in existing]
                ))
                sub.stock_codes = ",".join(merged)
                sub.enabled = True
                sub.updated_at = datetime.now()
                if message_id:
                    sub.message_id = message_id
                if chat_type:
                    sub.chat_type = chat_type
                session.commit()
            else:
                sub = AlertSubscription(
                    platform=platform,
                    user_id=user_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    stock_codes=stock_codes,
                    message_id=message_id,
                )
                session.add(sub)
                session.commit()
            session.refresh(sub)
            return sub
        except Exception as e:
            session.rollback()
            logger.error(f"[AlertSubscriptionRepo] upsert failed: {e}")
            raise
        finally:
            session.close()

    def get_active(self, user_id: str, chat_id: str) -> Optional[AlertSubscription]:
        session = self.db.get_session()
        try:
            return (
                session.query(AlertSubscription)
                .filter(
                    AlertSubscription.user_id == user_id,
                    AlertSubscription.chat_id == chat_id,
                    AlertSubscription.enabled == True,
                )
                .one_or_none()
            )
        finally:
            session.close()

    def list_all_active(self) -> List[AlertSubscription]:
        session = self.db.get_session()
        try:
            return (
                session.query(AlertSubscription)
                .filter(AlertSubscription.enabled == True)
                .all()
            )
        finally:
            session.close()

    def disable(self, user_id: str, chat_id: str) -> bool:
        session = self.db.get_session()
        try:
            sub = (
                session.query(AlertSubscription)
                .filter(
                    AlertSubscription.user_id == user_id,
                    AlertSubscription.chat_id == chat_id,
                    AlertSubscription.enabled == True,
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
            logger.error(f"[AlertSubscriptionRepo] disable failed: {e}")
            return False
        finally:
            session.close()

    def list_by_user(self, user_id: str) -> List[AlertSubscription]:
        session = self.db.get_session()
        try:
            return (
                session.query(AlertSubscription)
                .filter(AlertSubscription.user_id == user_id)
                .order_by(AlertSubscription.created_at.desc())
                .all()
            )
        finally:
            session.close()
