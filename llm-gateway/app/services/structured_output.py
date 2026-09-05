"""Structured-output prompt construction and local result validation."""
import json
from typing import Any

from jsonschema import ValidationError, validate

from app.data import ModelResult, ResponseFormat
from app.errors import StructuredOutputError


def prompt_instruction(response_format: ResponseFormat) -> str:
    """生成供不支持原生结构化输出的模型使用的附加指令。"""
    if response_format.type == "json_object":
        return "Return only one valid JSON object. Do not use Markdown fences or add any other text."
    if response_format.type == "json_schema":
        schema = json.dumps(response_format.json_schema, ensure_ascii=False, separators=(",", ":"))
        return (
            "Return only JSON that validates against this JSON Schema. "
            f"Do not use Markdown fences or add any other text. JSON Schema: {schema}"
        )
    return ""


def validate_result(result: ModelResult, response_format: ResponseFormat) -> Any | None:
    """校验结构化输出的 JSON 合法性及可选 JSON Schema。"""
    if response_format.type == "text":
        return None
    return validate_text(_output_text(result), response_format)


def validate_text(text: str, response_format: ResponseFormat) -> Any | None:
    """校验已聚合的输出文本，供非流式结果和 SSE 结束事件共同使用。"""
    if response_format.type == "text":
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"Model returned invalid JSON: {exc.msg}") from exc
    if response_format.type == "json_object" and not isinstance(parsed, dict):
        raise StructuredOutputError("Model response must be a JSON object")
    if response_format.type == "json_schema":
        try:
            validate(instance=parsed, schema=response_format.json_schema)
        except ValidationError as exc:
            raise StructuredOutputError(f"Model response does not match JSON Schema: {exc.message}") from exc
    return parsed


def _output_text(result: ModelResult) -> str:
    """从统一 Responses 风格结果中提取全部输出文本。"""
    chunks: list[str] = []
    for item in result.output:
        for content in item.get("content", []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "".join(chunks)
