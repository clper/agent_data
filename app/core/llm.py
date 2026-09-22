"""
LLM 客户端封装：OpenAI 兼容接口。

设计要点：
- 统一封装 chat_completion 调用
- 支持 DeepSeek / OpenAI / 任意兼容接口
- 内置重试 + 超时
- 返回结构化 JSON（要求模型输出 JSON 时）
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from app.config import LLMConfig

logger = logging.getLogger(__name__)


@dataclass
class ChatMessage:
    """聊天消息"""
    role: str                          # "system" | "user" | "assistant"
    content: str


class LLMClient:
    """
    LLM 调用客户端。

    为什么不直接在各模块里调用 OpenAI？
    → 统一管理 API Key、超时、重试
    → 便于 mock 测试
    → 切换模型只需改配置
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self.client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=config.timeout,
        )

    def chat(
        self,
        messages: list[ChatMessage],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """
        发送聊天请求，返回文本响应。

        Args:
            messages: 消息列表
            temperature: 覆盖默认温度
            max_tokens: 覆盖默认 max_tokens

        Returns:
            模型回复文本
        """
        msg_dicts = [{"role": m.role, "content": m.content} for m in messages]
        response = self.client.chat.completions.create(
            model=self.config.model,
            messages=msg_dicts,
            temperature=temperature if temperature is not None else self.config.temperature,
            max_tokens=max_tokens or self.config.max_tokens,
        )
        content = response.choices[0].message.content or ""
        logger.debug("LLM response: %s", content[:200])
        return content

    def chat_json(
        self,
        messages: list[ChatMessage],
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """
        发送聊天请求，期望模型返回 JSON。

        如果模型返回的不是合法 JSON，尝试从文本中提取 JSON 块。

        Returns:
            解析后的字典
        """
        text = self.chat(messages, temperature=temperature)
        return self._parse_json(text)

    def _parse_json(self, text: str) -> dict[str, Any]:
        """从模型输出中提取 JSON"""
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取 ```json ... ``` 代码块
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            try:
                return json.loads(text[start:end].strip())
            except (json.JSONDecodeError, ValueError):
                pass

        # 尝试提取 { ... } 块
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end != -1:
            try:
                return json.loads(text[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                pass

        logger.warning("Failed to parse JSON from LLM output: %s", text[:200])
        return {"error": "invalid_json", "raw": text}
