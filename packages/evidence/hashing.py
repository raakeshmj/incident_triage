"""Content hashing for evidence records.

`content_hash = "sha256:" + sha256(canonical_json(raw_response))`, where
canonical JSON is sorted-key, separator-compact UTF-8. Hashing the canonical
form of the *persisted* raw response (not the backend's exact bytes) is
deliberate: the stored JSONB is what an auditor can re-read, so the hash
must be recomputable from it -- `verify_content_hash` does exactly that. Key
order and whitespace are not content; values are.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

HASH_PREFIX = "sha256:"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(raw_response: Any) -> str:
    digest = hashlib.sha256(canonical_json(raw_response).encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"


def verify_content_hash(raw_response: Any, expected: str) -> bool:
    return content_hash(raw_response) == expected
