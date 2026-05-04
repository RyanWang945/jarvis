import pytest
from pathlib import Path

from utils.token_counter import (
    DEFAULT_DEEPSEEK_TOKENIZER_JSON,
    DeepSeekTokenCounter,
    TokenizerUnavailableError,
    estimate_text_tokens,
)


def test_bundled_deepseek_tokenizer_file_exists() -> None:
    assert DEFAULT_DEEPSEEK_TOKENIZER_JSON.exists()


def test_counter_returns_fallback_estimate_when_tokenizer_unavailable() -> None:
    text = "Hello, Jarvis"
    counter = DeepSeekTokenCounter(Path("missing-tokenizer.json"))

    assert counter.count_text(text) == estimate_text_tokens(text)


def test_count_text_tokens_can_require_exact_tokenizer() -> None:
    counter = DeepSeekTokenCounter(Path("missing-tokenizer.json"))

    with pytest.raises(TokenizerUnavailableError):
        counter.count_text("Hello", fallback_to_estimate=False)
