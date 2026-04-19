from app.llm.client import ChatClient, LLMMessage
from app.llm.jarvis import JarvisLLM


class DeepSeekClient(JarvisLLM):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout_seconds: float = 180.0,
    ) -> None:
        super().__init__(
            ChatClient(
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout_seconds=timeout_seconds,
            )
        )


__all__ = ["DeepSeekClient", "LLMMessage"]
