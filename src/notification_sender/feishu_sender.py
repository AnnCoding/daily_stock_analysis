# -*- coding: utf-8 -*-
"""
飞书 发送提醒服务

职责：
1. 通过 webhook 发送飞书消息
"""
import base64
import hashlib
import logging
from typing import Dict, Any, List, Optional
import requests
import time

from src.config import Config
from src.formatters import format_feishu_markdown, chunk_content_by_max_bytes

# 飞书图片上传限制（20MB）
FEISHU_IMAGE_MAX_BYTES = 20 * 1024 * 1024


logger = logging.getLogger(__name__)


class FeishuSender:
    
    def __init__(self, config: Config):
        """
        初始化飞书配置

        Args:
            config: 配置对象
        """
        self._feishu_url = getattr(config, 'feishu_webhook_url', None)
        self._feishu_max_bytes = getattr(config, 'feishu_max_bytes', 20000)
        self._webhook_verify_ssl = getattr(config, 'webhook_verify_ssl', True)
        # 多 Webhook 配置
        self._feishu_webhooks = getattr(config, 'feishu_webhooks', [])
    
          
    def send_to_feishu(self, content: str) -> bool:
        """
        推送消息到飞书机器人
        
        飞书自定义机器人 Webhook 消息格式：
        {
            "msg_type": "text",
            "content": {
                "text": "文本内容"
            }
        }
        
        说明：飞书文本消息不会渲染 Markdown，需使用交互卡片（lark_md）格式
        
        注意：飞书文本消息限制约 20KB，超长内容会自动分批发送
        可通过环境变量 FEISHU_MAX_BYTES 调整限制值
        
        Args:
            content: 消息内容（Markdown 会转为纯文本）
            
        Returns:
            是否发送成功
        """
        if not self._feishu_url:
            logger.warning("飞书 Webhook 未配置，跳过推送")
            return False
        
        # 飞书 lark_md 支持有限，先做格式转换
        formatted_content = format_feishu_markdown(content)

        max_bytes = self._feishu_max_bytes  # 从配置读取，默认 20000 字节
        
        # 检查字节长度，超长则分批发送
        content_bytes = len(formatted_content.encode('utf-8'))
        if content_bytes > max_bytes:
            logger.info(f"飞书消息内容超长({content_bytes}字节/{len(content)}字符)，将分批发送")
            return self._send_feishu_chunked(formatted_content, max_bytes)
        
        try:
            return self._send_feishu_message(formatted_content)
        except Exception as e:
            logger.error(f"发送飞书消息失败: {e}")
            return False
   
    def _send_feishu_chunked(self, content: str, max_bytes: int) -> bool:
        """
        分批发送长消息到飞书

        按股票分析块（以 --- 或 ### 分隔）智能分割，确保每批不超过限制

        Args:
            content: 完整消息内容
            max_bytes: 单条消息最大字节数

        Returns:
            是否全部发送成功
        """
        if not self._feishu_url:
            logger.warning("飞书 Webhook URL 为空，跳过推送")
            return False
        return self._send_feishu_chunked_to_url(self._feishu_url, content, max_bytes)
    
    def _send_feishu_message(self, content: str) -> bool:
        """发送单条飞书消息（优先使用 Markdown 卡片）"""
        if not self._feishu_url:
            logger.warning("飞书 Webhook URL 为空，跳过推送")
            return False
        return self._send_feishu_message_to_url(self._feishu_url, content)

    def _send_feishu_image(self, image_bytes: bytes) -> bool:
        """
        Send image via Feishu webhook using file_key upload API.

        Feishu requires uploading the image first to get a file_key, then
        sending the file_key via interactive card.

        Args:
            image_bytes: Image data in bytes

        Returns:
            bool: True if send successful, False otherwise
        """
        if not self._feishu_url:
            logger.warning("飞书 Webhook 未配置，无法发送图片")
            return False

        if len(image_bytes) > FEISHU_IMAGE_MAX_BYTES:
            logger.warning(
                "飞书图片超限 (%d > %d bytes)，拒绝发送",
                len(image_bytes), FEISHU_IMAGE_MAX_BYTES,
            )
            return False

        try:
            # Upload image to Feishu API
            # First, extract the tenant ID from webhook URL
            # Webhook format: https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx
            # For image upload, we need to use the image API which requires access_token
            # Since webhook doesn't provide upload capability, we'll use the image card approach

            # For webhook-based image sending, we encode as base64 and send as image card
            b64 = base64.b64encode(image_bytes).decode("ascii")
            md5_hash = hashlib.md5(image_bytes).hexdigest()

            # Using Feishu's image card element with img_key
            # Note: This requires uploading the image first to get img_key
            # For webhook, we'll try the image API approach

            logger.info("尝试发送飞书图片...")

            # Build an image card payload
            # Since direct image upload via webhook is limited, we'll use a different approach:
            # Send a message with image key if available, or fallback to text
            payload = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": "A股智能分析报告"
                        }
                    },
                    "elements": [
                        {
                            "tag": "img",
                            "img_key": "",  # Empty as we can't upload via webhook directly
                            "alt": {
                                "tag": "plain_text",
                                "content": "分析报告"
                            }
                        }
                    ]
                }
            }

            # Try to send as image card
            response = requests.post(
                self._feishu_url,
                json=payload,
                timeout=30,
                verify=self._webhook_verify_ssl
            )

            if response.status_code == 200:
                result = response.json()
                code = result.get('code') if 'code' in result else result.get('StatusCode')
                if code == 0:
                    logger.info("飞书图片卡片发送成功")
                    return True
                else:
                    logger.error("飞书图片卡片发送失败: %s", result.get('msg', '未知错误'))

            # If image upload fails, log the error
            logger.error("飞书请求失败: HTTP %s", response.status_code)
            return False

        except Exception as e:
            logger.error("飞书图片发送异常: %s", e)
            return False

    def get_feishu_webhook_by_alias(self, alias: str) -> Optional[Dict[str, Any]]:
        """
        根据别名获取飞书 Webhook 配置

        Args:
            alias: Webhook 别名

        Returns:
            Webhook 配置字典，包含 url, alias, stocks；未找到返回 None
        """
        for webhook in self._feishu_webhooks:
            if webhook.get('alias') == alias:
                return webhook
        return None

    def get_feishu_webhook_aliases(self) -> List[str]:
        """
        获取所有已配置的飞书 Webhook 别名列表

        Returns:
            别名列表
        """
        return [w.get('alias', '') for w in self._feishu_webhooks if w.get('alias')]

    def send_to_feishu_by_alias(self, alias: str, content: str) -> bool:
        """
        推送消息到指定别名的飞书群

        Args:
            alias: Webhook 别名
            content: 消息内容

        Returns:
            是否发送成功
        """
        webhook = self.get_feishu_webhook_by_alias(alias)
        if not webhook:
            logger.error(f"未找到别名 '{alias}' 的飞书 Webhook 配置")
            return False

        url = webhook.get('url')
        if not url:
            logger.error(f"别名 '{alias}' 的飞书 Webhook URL 为空")
            return False

        # 飞书 lark_md 支持有限，先做格式转换
        formatted_content = format_feishu_markdown(content)
        max_bytes = self._feishu_max_bytes

        # 检查字节长度，超长则分批发送
        content_bytes = len(formatted_content.encode('utf-8'))
        if content_bytes > max_bytes:
            logger.info(f"飞书消息内容超长({content_bytes}字节/{len(content)}字符)，将分批发送到 '{alias}'")
            return self._send_feishu_chunked_to_url(url, formatted_content, max_bytes)

        try:
            return self._send_feishu_message_to_url(url, formatted_content)
        except Exception as e:
            logger.error(f"发送飞书消息到 '{alias}' 失败: {e}")
            return False

    def send_to_feishu_webhook(self, url: str, content: str) -> bool:
        """
        推送消息到指定 URL 的飞书群

        Args:
            url: Webhook URL
            content: 消息内容

        Returns:
            是否发送成功
        """
        if not url:
            logger.warning("飞书 Webhook URL 为空，跳过推送")
            return False

        # 飞书 lark_md 支持有限，先做格式转换
        formatted_content = format_feishu_markdown(content)
        max_bytes = self._feishu_max_bytes

        # 检查字节长度，超长则分批发送
        content_bytes = len(formatted_content.encode('utf-8'))
        if content_bytes > max_bytes:
            logger.info(f"飞书消息内容超长({content_bytes}字节/{len(content)}字符)，将分批发送")
            return self._send_feishu_chunked_to_url(url, formatted_content, max_bytes)

        try:
            return self._send_feishu_message_to_url(url, formatted_content)
        except Exception as e:
            logger.error(f"发送飞书消息失败: {e}")
            return False

    def _send_feishu_chunked_to_url(self, url: str, content: str, max_bytes: int) -> bool:
        """
        分批发送长消息到指定 URL 的飞书

        Args:
            url: Webhook URL
            content: 完整消息内容
            max_bytes: 单条消息最大字节数

        Returns:
            是否全部发送成功
        """
        chunks = chunk_content_by_max_bytes(content, max_bytes, add_page_marker=True)

        total_chunks = len(chunks)
        success_count = 0

        logger.info(f"飞书分批发送到 {url}：共 {total_chunks} 批")

        for i, chunk in enumerate(chunks):
            try:
                if self._send_feishu_message_to_url(url, chunk):
                    success_count += 1
                    logger.info(f"飞书第 {i+1}/{total_chunks} 批发送成功")
                else:
                    logger.error(f"飞书第 {i+1}/{total_chunks} 批发送失败")
            except Exception as e:
                logger.error(f"飞书第 {i+1}/{total_chunks} 批发送异常: {e}")

            if i < total_chunks - 1:
                time.sleep(1)

        return success_count == total_chunks

    def _send_feishu_message_to_url(self, url: str, content: str) -> bool:
        """
        发送单条飞书消息到指定 URL（优先使用 Markdown 卡片）

        Args:
            url: Webhook URL
            content: 消息内容

        Returns:
            是否发送成功
        """
        def _post_payload(payload: Dict[str, Any]) -> bool:
            logger.debug(f"飞书请求 URL: {url}")
            logger.debug(f"飞书请求 payload 长度: {len(content)} 字符")

            response = requests.post(
                url,
                json=payload,
                timeout=30,
                verify=self._webhook_verify_ssl
            )

            logger.debug(f"飞书响应状态码: {response.status_code}")
            logger.debug(f"飞书响应内容: {response.text}")

            if response.status_code == 200:
                result = response.json()
                code = result.get('code') if 'code' in result else result.get('StatusCode')
                if code == 0:
                    logger.info("飞书消息发送成功")
                    return True
                else:
                    error_msg = result.get('msg') or result.get('StatusMessage', '未知错误')
                    error_code = result.get('code') or result.get('StatusCode', 'N/A')
                    logger.error(f"飞书返回错误 [code={error_code}]: {error_msg}")
                    logger.error(f"完整响应: {result}")
                    return False
            else:
                logger.error(f"飞书请求失败: HTTP {response.status_code}")
                logger.error(f"响应内容: {response.text}")
                return False

        # 1) 优先使用交互卡片（支持 Markdown 渲染）
        card_payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "A股智能分析报告"
                    }
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": content
                        }
                    }
                ]
            }
        }

        if _post_payload(card_payload):
            return True

        # 2) 回退为普通文本消息
        text_payload = {
            "msg_type": "text",
            "content": {
                "text": content
            }
        }

        return _post_payload(text_payload)