from __future__ import annotations

import argparse
from functools import lru_cache
from pathlib import Path
from typing import Any


class TokenizerUnavailableError(RuntimeError):
    """Raised when the local DeepSeek tokenizer cannot be loaded."""


DEFAULT_DEEPSEEK_TOKENIZER_JSON = (
    Path(__file__).resolve().parent
    / "deepseek_v3_tokenizer"
    / "deepseek_v3_tokenizer"
    / "tokenizer.json"
)


def estimate_text_tokens(text: str) -> int:
    """Fast fallback estimate used when the tokenizer package is unavailable."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


class DeepSeekTokenCounter:
    """Token counter backed by the bundled DeepSeek V3 tokenizer.json."""

    def __init__(self, tokenizer_json_path: Path | str = DEFAULT_DEEPSEEK_TOKENIZER_JSON) -> None:
        self._tokenizer_json_path = Path(tokenizer_json_path)
        self._tokenizer: Any | None = None

    @property
    def tokenizer_json_path(self) -> Path:
        return self._tokenizer_json_path

    def count_text(self, text: str, *, fallback_to_estimate: bool = True) -> int:
        if not text:
            return 0
        try:
            tokenizer = self._load_tokenizer()
        except TokenizerUnavailableError:
            if fallback_to_estimate:
                return estimate_text_tokens(text)
            raise
        return len(tokenizer.encode(text).ids)

    def _load_tokenizer(self) -> Any:
        if self._tokenizer is not None:
            return self._tokenizer
        if not self._tokenizer_json_path.exists():
            raise TokenizerUnavailableError(f"DeepSeek tokenizer not found: {self._tokenizer_json_path}")
        try:
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise TokenizerUnavailableError(
                "Python package 'tokenizers' is required for exact DeepSeek token counting."
            ) from exc
        self._tokenizer = Tokenizer.from_file(str(self._tokenizer_json_path))
        return self._tokenizer


@lru_cache(maxsize=1)
def get_deepseek_token_counter() -> DeepSeekTokenCounter:
    return DeepSeekTokenCounter()


def count_text_tokens(text: str, *, fallback_to_estimate: bool = True) -> int:
    """Count tokens for plain text with the bundled DeepSeek tokenizer."""
    return get_deepseek_token_counter().count_text(text, fallback_to_estimate=fallback_to_estimate)


def main() -> None:
    parser = argparse.ArgumentParser(description="Count text tokens with the bundled DeepSeek tokenizer.")
    parser.add_argument("text", help="Text to count.")
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="Fail if the exact tokenizer cannot be loaded instead of using the heuristic estimate.",
    )
    args = parser.parse_args()
    print(count_text_tokens(args.text, fallback_to_estimate=not args.no_fallback))


if __name__ == "__main__":
    main()
