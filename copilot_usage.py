"""Extract token counts from OTLP trace exports without retaining chat content."""
from __future__ import annotations

import re
from typing import Any


TOKEN_FIELDS = {
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "cache_read_tokens": "gen_ai.usage.cache_read.input_tokens",
    "cache_creation_tokens": "gen_ai.usage.cache_creation.input_tokens",
}


def _attributes(items: Any) -> dict[str, Any]:
    if not isinstance(items, list):
        raise ValueError("OTLP attributes must be an array.")
    result = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            raise ValueError("Invalid OTLP attribute.")
        key = item["key"]
        if key in result:
            raise ValueError(f"Duplicate OTLP attribute: {key}")
        result[key] = item.get("value")
    return result


def _count(value: Any, key: str) -> int:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid token count for {key}.")
    count = value.get("intValue")
    if isinstance(count, str) and re.fullmatch(r"[0-9]+", count):
        count = int(count)
    if type(count) is not int or count < 0:
        raise ValueError(f"Expected a nonnegative OTLP intValue for {key}.")
    return count


def extract_requests(document: Any) -> tuple[list[dict], int]:
    """Read standard GenAI token attributes from OTLP resourceSpans."""
    if not isinstance(document, dict) or not isinstance(document.get("resourceSpans"), list):
        raise ValueError("Expected an OTLP JSON trace export with resourceSpans.")
    requests = {}
    missing_usage = 0
    for resource in document["resourceSpans"]:
        if not isinstance(resource, dict) or not isinstance(resource.get("scopeSpans"), list):
            raise ValueError("Expected OTLP scopeSpans.")
        for scope in resource["scopeSpans"]:
            if not isinstance(scope, dict) or not isinstance(scope.get("spans"), list):
                raise ValueError("Expected OTLP spans.")
            for span in scope["spans"]:
                if not isinstance(span, dict):
                    raise ValueError("Invalid OTLP span.")
                attributes = _attributes(span.get("attributes", []))
                is_request = any(key in attributes for key in (
                    *TOKEN_FIELDS.values(), "gen_ai.request.model", "gen_ai.response.model",
                ))
                if not is_request:
                    continue
                trace, identifier = span.get("traceId"), span.get("spanId")
                if not isinstance(trace, str) or not isinstance(identifier, str) or not trace or not identifier:
                    raise ValueError("GenAI spans require traceId and spanId for deduplication.")
                model_value = attributes.get(
                    "gen_ai.response.model", attributes.get("gen_ai.request.model", {})
                )
                model = model_value.get("stringValue") if isinstance(model_value, dict) else None
                if model is not None and not isinstance(model, str):
                    raise ValueError("Invalid model stringValue.")
                record = {"trace_id": trace, "span_id": identifier, "model": model or "unreported"}
                for field, key in TOKEN_FIELDS.items():
                    record[field] = _count(attributes[key], key) if key in attributes else None
                identity = (trace, identifier)
                if identity in requests and requests[identity] != record:
                    raise ValueError("Conflicting token records for the same OTLP span.")
                requests[identity] = record
    for record in requests.values():
        if record["input_tokens"] is None or record["output_tokens"] is None:
            missing_usage += 1
    if not requests:
        raise ValueError(
            "No supported GenAI request spans found. Export format may be unsupported; "
            "no zero-usage result has been recorded."
        )
    return list(requests.values()), missing_usage


def summarize(requests: list[dict]) -> dict:
    models: dict[str, dict] = {}
    for record in requests:
        totals = models.setdefault(record["model"], {
            "requests": 0, **{field: {"recorded": 0, "missing": 0} for field in TOKEN_FIELDS},
        })
        totals["requests"] += 1
        for field in TOKEN_FIELDS:
            value = record[field]
            if value is None:
                totals[field]["missing"] += 1
            else:
                totals[field]["recorded"] += value
    return models
