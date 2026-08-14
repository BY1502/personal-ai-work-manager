from __future__ import annotations

import json

from app.providers import StructuredOutputTransport
from app.recommendation import (
    RecommendationNarrationEnvelope,
    RecommendationNarrationRequest,
)


RECOMMENDATION_NARRATION_PROMPT_VERSION = "recommendation-narration-ko-v1"
REPORT_NARRATION_PROMPT_VERSION = "report-narration-ko-v1"


class LLMRecommendationNarrator:
    """Presentation-only adapter over the selected Local/API transport."""

    def __init__(self, transport: StructuredOutputTransport) -> None:
        self.transport = transport
        self.provider_name = transport.provider_name
        self.model_name = transport.model_name
        self.version = (
            f"{self.provider_name}:{self.model_name}:"
            f"{RECOMMENDATION_NARRATION_PROMPT_VERSION}"
        )

    def narrate(
        self,
        request: RecommendationNarrationRequest,
    ) -> RecommendationNarrationEnvelope:
        schema = RecommendationNarrationEnvelope.model_json_schema()
        raw = self.transport.complete_json(
            schema_name="recommendation_narration_v1",
            schema=schema,
            system_prompt=(
                "당신은 개인 업무 매니저의 추천 이유를 짧고 자연스러운 한국어로 "
                "표현합니다. 입력의 순서와 work_item_id를 그대로 유지하세요. "
                "입력에 없는 프로젝트, 업무, 상태, 날짜, 사람, 행동을 추가하지 "
                "마세요. 순위를 바꾸거나 새 추천을 만들지 마세요. 설명과 Markdown "
                "없이 recommendation-narration.v1 JSON만 반환하세요.\n"
                "JSON Schema:\n"
                + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            ),
            user_prompt=json.dumps(
                request.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return RecommendationNarrationEnvelope.model_validate_json(raw)


class LLMReportNarrator:
    """Grounded report copy adapter; ReportManager validates every fact binding."""

    def __init__(self, transport: StructuredOutputTransport) -> None:
        self.transport = transport
        self.provider_name = transport.provider_name
        self.model_name = transport.model_name
        self.prompt_version = REPORT_NARRATION_PROMPT_VERSION

    def narrate(self, payload: dict) -> dict:
        schema = _report_narration_schema()
        raw = self.transport.complete_json(
            schema_name="report_narration_v1",
            schema=schema,
            system_prompt=(
                "당신은 구조화된 업무 보고서의 문장만 짧고 자연스러운 한국어로 "
                "다듬습니다. source_digest, bullet 순서, bullet_id, fact_ids를 입력과 "
                "정확히 같게 유지하세요. 입력에 없는 프로젝트, 업무, 활동, 상태, "
                "날짜, 사람, 숫자, 결과를 추가하지 마세요. 설명과 Markdown 없이 "
                "report-narration.v1 JSON만 반환하세요.\nJSON Schema:\n"
                + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            ),
            user_prompt=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return _strict_json_object(raw)


def recommendation_narrator_for(extractor) -> LLMRecommendationNarrator | None:
    transport = getattr(extractor, "transport", None)
    if transport is None:
        return None
    return LLMRecommendationNarrator(transport)


def report_narrator_for(extractor) -> LLMReportNarrator | None:
    transport = getattr(extractor, "transport", None)
    if transport is None:
        return None
    return LLMReportNarrator(transport)


def _report_narration_schema() -> dict:
    bullet = {
        "type": "object",
        "properties": {
            "bullet_id": {"type": "string", "minLength": 1, "maxLength": 500},
            "fact_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 500},
                "maxItems": 200,
            },
            "text": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "required": ["bullet_id", "fact_ids", "text"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "const": "report-narration.v1"},
            "source_digest": {"type": "string", "minLength": 64, "maxLength": 64},
            "bullets": {"type": "array", "items": bullet, "maxItems": 500},
        },
        "required": ["schema_version", "source_digest", "bullets"],
        "additionalProperties": False,
    }


def _strict_json_object(raw: str) -> dict:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_: str):
        raise ValueError("non-finite JSON constant")

    parsed = json.loads(
        raw,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    if not isinstance(parsed, dict):
        raise ValueError("narration output must be a JSON object")
    return parsed
