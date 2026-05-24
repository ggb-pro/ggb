"""LLM service: stream responses via OpenAI-compatible API."""

import json
import httpx
from app.config import get_settings
from app.schemas.chat import ChatMessage

settings = get_settings()

SYSTEM_PROMPT = """你是一个专业的知识库助手。基于以下参考信息回答用户的问题。

## 严格规则
1. 只使用参考信息中的内容回答，不要编造信息
2. 如果参考信息不足以回答问题，明确说明"参考信息中没有相关内容"
3. 每个事实性观点后标注来源编号，格式：[1] [2]
4. 如果不同来源有矛盾观点，都列出来并标注各自来源
5. 回答使用项目符号或数字列表组织

## 参考信息
{context}"""


class LLMService:
    async def stream_generate(
        self,
        query: str,
        context: str,
        history: list[ChatMessage] | None = None,
    ):
        """Stream tokens from LLM API via SSE."""
        messages = []

        if history:
            for msg in history[-6:]:
                messages.append({"role": msg.role, "content": msg.content})

        messages.append({"role": "user", "content": query})
        system = SYSTEM_PROMPT.format(context=context) if context else "你是一个知识库助手。"

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                f"{settings.llm_api_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={
                    "model": settings.llm_model,
                    "messages": [{"role": "system", "content": system}] + messages,
                    "stream": True,
                    "max_tokens": 2000,
                    "temperature": 0.3,
                },
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        # glm-5.1 has reasoning_content (chain of thought)
                        reasoning = delta.get("reasoning_content", "")
                        token = delta.get("content", "")
                        if token:
                            yield token
                    except json.JSONDecodeError:
                        continue
