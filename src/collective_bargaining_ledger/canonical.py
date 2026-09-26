"""规范化序列化与内容哈希。

“逐字一致”只能建立在确定性编码之上：键序、空白、Unicode 形式任一不同，
都可能让两份语义相同的文本得到不同哈希，或让两份异文得到相同结论。
所有需要双方逐字确认的版本，都先经过本模块编码再计算哈希。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    """返回确定性的 UTF-8 字节：键排序、无多余空白、不允许 NaN。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json(value: Any) -> str:
    """确定性 JSON 字符串，用于落库与展示。"""

    return canonical_bytes(value).decode("utf-8")


def content_hash(value: Any) -> str:
    """对规范化后的内容计算 SHA-256 十六进制摘要。"""

    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def short_hash(value: str) -> str:
    """仅用于展示的短代号，不承担一致性判定职责。"""

    return value[:10]
