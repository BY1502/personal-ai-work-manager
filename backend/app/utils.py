from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now_utc(self) -> datetime: ...


class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._value = value.astimezone(timezone.utc)

    def now_utc(self) -> datetime:
        return self._value

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._value = value.astimezone(timezone.utc)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def local_date(clock: Clock, timezone_name: str) -> date:
    return clock.now_utc().astimezone(ZoneInfo(timezone_name)).date()


def resolve_local_date(expression: str, *, today: date) -> date:
    normalized = expression.strip().upper()
    if normalized == "TODAY":
        return today
    if normalized == "YESTERDAY":
        return date.fromordinal(today.toordinal() - 1)
    # The extraction contract stores the date of the recorded activity, not a
    # due-date field.  Local models sometimes emit a relative future token for
    # a sentence such as "다음 주에 전달할 거야".  Keeping that token as a
    # validation error makes ordinary planning notes fail with HTTP 422 and
    # loses the user's work context.  Until a first-class due-date field is
    # added, treat the explicitly-known future planning tokens as recorded
    # today.  Unknown tokens still fail closed below.
    if normalized in {"TOMORROW", "NEXT_WEEK", "NEXT_MONTH", "LATER", "FUTURE"}:
        return today
    return date.fromisoformat(expression)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def normalize_name(value: str) -> str:
    compact = re.sub(r"\s+", "", value.strip().casefold())
    return re.sub(r"[^\w가-힣]", "", compact)


def topic_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[A-Za-z0-9가-힣]{2,}", value.casefold())
        if token
        not in {
            "오늘",
            "어제",
            "아까",
            "그거",
            "답변",
            "왔어",
            "했고",
            "했어",
            "해놨어",
            "지금",
            "어디까지",
        }
    }
