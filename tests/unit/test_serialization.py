"""序列化测试：JSON 往返相等、版本字段、时间格式、schema 可生成、无 Any 泄漏。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from tests.conftest import (
    FIXED_NOW,
    make_asset,
    make_file_record,
    make_integrity_report,
    make_metadata,
    make_request,
    make_source_metadata,
    make_task,
    make_transfer_result,
)

from researchdatahub.core.models import (
    MODELS_VERSION,
    AcquisitionPlan,
    AcquisitionTask,
    DatasetMetadata,
    DatasetRequest,
    DeliveryManifest,
    FileRecord,
    IntegrityReport,
    SourceAsset,
    SourceMetadata,
    TransferResult,
)

ALL_MODELS = [
    DatasetRequest,
    SourceAsset,
    AcquisitionPlan,
    AcquisitionTask,
    TransferResult,
    FileRecord,
    IntegrityReport,
    DeliveryManifest,
    SourceMetadata,
    DatasetMetadata,
]


def sample_instances() -> list[object]:
    """每个模型一个真实实例。"""
    metadata = make_metadata()
    record = make_file_record()
    return [
        make_request(),
        make_asset(),
        AcquisitionPlan(
            plan_id="plan-0001",
            request_id="req-era5-clwc-2011-2020",
            tasks=[make_task()],
            generated_at=FIXED_NOW,
        ),
        make_task(),
        make_transfer_result(),
        record,
        make_integrity_report(),
        DeliveryManifest(
            delivery_id="delivery-0001",
            request_id="req-era5-clwc-2011-2020",
            plan_id="plan-0001",
            dataset_metadata=metadata,
            files=[record],
            integrity=[make_integrity_report()],
            created_at=FIXED_NOW,
            tool_version="0.1.0",
        ).with_totals(),
        make_source_metadata(),
        metadata,
    ]


SAMPLES = list(zip(ALL_MODELS, sample_instances(), strict=True))


@pytest.mark.parametrize(("model", "instance"), SAMPLES, ids=[m.__name__ for m in ALL_MODELS])
def test_json_roundtrip_is_lossless(model: type, instance: object) -> None:
    """model_dump_json → json.loads → model_validate 必须得到完全相等的对象。"""
    dumped = instance.model_dump_json()  # type: ignore[attr-defined]
    parsed = json.loads(dumped)
    assert isinstance(parsed, dict)
    restored = model.model_validate(parsed)  # type: ignore[attr-defined]
    assert restored == instance
    assert restored.model_dump_json() == dumped, "二次序列化结果必须稳定"


SAMPLES = list(zip(ALL_MODELS, sample_instances(), strict=True))


@pytest.mark.parametrize(("model", "instance"), SAMPLES, ids=[m.__name__ for m in ALL_MODELS])
def test_schema_version_present_and_current(model: type, instance: object) -> None:
    payload = instance.model_dump(mode="json")  # type: ignore[attr-defined]
    assert payload["schema_version"] == MODELS_VERSION
    assert MODELS_VERSION == "1.0"


@pytest.mark.parametrize("model", ALL_MODELS, ids=[m.__name__ for m in ALL_MODELS])
def test_json_schema_generates_and_forbids_extra(model: type) -> None:
    """JSON schema 可生成，且明确禁止额外字段（契约的可交换基础）。"""
    schema = model.model_json_schema()
    assert schema["title"] or schema.get("$defs")
    assert schema.get("additionalProperties") is False


def test_datetimes_are_iso8601_utc() -> None:
    task = make_task()
    transition_note = "2026-11-04T12:00:00Z"
    from researchdatahub.core.state import TaskState, transition

    running = transition(task, TaskState.PLANNED, reason="plan", now=FIXED_NOW)
    payload = json.loads(running.model_dump_json())
    assert payload["last_transition_at"] == transition_note
    assert payload["schema_version"] == "1.0"
    assert running.model_dump(mode="python")["last_transition_at"].tzinfo is UTC


def test_naive_datetime_rejected_on_parse() -> None:
    """反序列化时也强制 tz-aware，避免不同来源混入本地时间。"""
    from pydantic import ValidationError

    payload = json.loads(make_source_metadata().model_dump_json())
    payload["retrieved_at"] = "2026-11-04T12:00:00"  # 无时区
    with pytest.raises(ValidationError, match="timezone-aware"):
        SourceMetadata.model_validate(payload)


def test_to_json_dict_matches_model_dump_json() -> None:
    metadata = make_metadata()
    assert metadata.to_json_dict() == json.loads(metadata.model_dump_json())


def test_fingerprint_is_stable_and_content_addressed() -> None:
    a = make_metadata()
    b = make_metadata()
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint().startswith("sha256-16:")
    changed = make_metadata(data_version="e5.2")
    assert changed.fingerprint() != a.fingerprint()


def test_no_untyped_any_in_public_schemas() -> None:
    """禁止用 Any 逃避类型设计：公开 schema 里不得出现裸 object/{} 字段类型。"""

    def walk(node: object, path: str = "$") -> list[tuple[str, object]]:
        found: list[tuple[str, object]] = []
        if isinstance(node, dict):
            is_untyped_object = (
                node.get("type") == "object"
                and "additionalProperties" not in node
                and "properties" not in node
            )
            if is_untyped_object:
                found.append((path, node))
            elif "properties" in node:
                for key, value in node.items():
                    found.extend(walk(value, f"{path}.{key}"))
            else:
                for key, value in node.items():
                    found.extend(walk(value, f"{path}.{key}"))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                found.extend(walk(value, f"{path}[{index}]"))
        return found

    for model in ALL_MODELS:
        schema = model.model_json_schema()
        untyped = walk(schema)
        assert not untyped, f"{model.__name__} 存在未约束的 object 字段: {untyped[:3]}"


def test_extensions_are_typed_as_json_values() -> None:
    """extensions 的 schema 必须引用有界 JsonValue 定义，而不是 ``additionalProperties: true``。"""
    schema = DatasetMetadata.model_json_schema()
    extensions = schema["properties"]["extensions"]
    additional = extensions["additionalProperties"]
    assert additional is not True
    assert additional == {"$ref": "#/$defs/JsonValue"}
    json_value = schema["$defs"]["JsonValue"]
    rendered = json.dumps(json_value)
    assert "anyOf" in rendered, "JsonValue 应当是显式的 JSON 值联合"


def test_enum_values_serialize_as_strings() -> None:
    task = make_task()
    payload = json.loads(task.model_dump_json())
    assert payload["state"] == "PENDING"
    assert isinstance(payload["state"], str)
    assert payload["priority"] == "normal"


def test_json_compatible_with_utc_now() -> None:
    """时间字段在 JSON 里是可被 datetime.fromisoformat 解析的 ISO8601。"""
    record = make_file_record(retrieved_at=datetime(2026, 11, 4, 20, 0, tzinfo=UTC))
    payload = json.loads(record.model_dump_json())
    parsed = datetime.fromisoformat(payload["retrieved_at"].replace("Z", "+00:00"))
    assert parsed == datetime(2026, 11, 4, 20, 0, tzinfo=UTC)
