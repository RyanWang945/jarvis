from __future__ import annotations

import re


_TOKEN_USAGE_FOOTER_PATTERN = re.compile(
    r"""
    (?P<body>.*?)
    (?:\n{2,}|\A)
    (?:---\s*\n)?
    -?\s*模型：`?[^`\n]+`?\s*
    \n-?\s*Token：输入\s*`?\d+`?\s*/\s*输出\s*`?\d+`?\s*/\s*合计\s*`?\d+`?\s*
    \Z
    """,
    flags=re.DOTALL | re.VERBOSE,
)

_TOKEN_USAGE_INLINE_PATTERN = re.compile(
    r"""
    (?P<body>.*?)
    (?:\n{2,}|\A)
    模型：[^·\n]+·\s*Token：输入\s*\d+\s*/\s*输出\s*\d+\s*/\s*合计\s*\d+\s*
    \Z
    """,
    flags=re.DOTALL | re.VERBOSE,
)


def strip_token_usage_footer(content: str) -> str:
    text = str(content or "").rstrip()
    while True:
        match = _TOKEN_USAGE_FOOTER_PATTERN.match(text) or _TOKEN_USAGE_INLINE_PATTERN.match(text)
        if match is None:
            return text
        text = match.group("body").rstrip()
