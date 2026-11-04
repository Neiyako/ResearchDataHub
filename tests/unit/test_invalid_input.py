"""非法输入测试：每一条必须被拒绝，且错误信息指向正确字段。

用例数据来自 ``tests/fixtures/invalid/*.yaml``，新增用例无需改代码。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from tests.conftest import (
    FIXTURES,
    make_asset,
    make_file_record,
    make_integrity_report,
    make_metadata,
    make_request,
    make_task,
    make_transfer_result,
)

from researchdatahub.core.models import (
    AcquisitionTask,
    DatasetMetadata,
    DatasetRequest,
    DeliveryManifest,
)


def _load(name: str) -> list[dict[str, Any]]:
    return yaml.safe_load((FIXTURES / "invalid" / name).read_text(encoding="utf-8"))


def _apply(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    merged.update(patch)
    return merged


def _metadata_payload() -> dict[str, Any]:
    from tests.conftest import load_json

    return dict(load_json("era5_request.json")["metadata"])


def _request_payload() -> dict[str, Any]:
    from tests.conftest import load_json

    return dict(load_json("era5_request.json"))


CASES = _load("cases.yaml")
RECORD_CASES = _load("records.yaml")


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_invalid_dataset_inputs_rejected(case: dict[str, Any]) -> None:
    """数据集/需求层面的非法输入必须抛 ValidationError 且信息含目标字段。"""
    if case["kind"] == "metadata":
        payload = _apply(_metadata_payload(), case["patch"])
        with pytest.raises(ValidationError) as excinfo:
            DatasetMetadata.model_validate(payload)
    elif case["kind"] == "request":
        payload = _apply(_request_payload(), case["patch"])
        with pytest.raises(ValidationError) as excinfo:
            DatasetRequest.model_validate(payload)
    else:  # pragma: no cover - fixture 写错时给出明确失败
        pytest.fail(f"未知 case kind: {case['kind']}")

    assert case["expect_field"] in str(excinfo.value), (
        f"错误信息应指向 {case['expect_field']!r}，实际: {excinfo.value}"
    )


@pytest.mark.parametrize("case", RECORD_CASES, ids=[c["name"] for c in RECORD_CASES])
def _build_invalid_record(kind: str, patch: dict[str, Any]) -> object:
    """按 case kind 构造对象；构造本身应当抛 ValidationError。"""
    if kind == "asset":
        meta_source = patch.pop("metadata_source_id", None)
        overrides = dict(patch)
        if meta_source is not None:
            from tests.conftest import make_source_metadata

            # 故意让 asset.source_id 固定为 ncar，而 metadata 指向其它源 → 必须被拒。
            overrides = {
                **overrides,
                "source_id": "ncar",
                "metadata": make_source_metadata(
                    source_id=meta_source, origin_uri="https://example.org/x"
                ),
            }
        return make_asset(**overrides)
    if kind == "task":
        normalized = {
            key: (
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                if isinstance(value, str) and value.endswith("Z")
                else value
            )
            for key, value in patch.items()
        }
        return make_task(**normalized)
    if kind == "transfer":
        return make_transfer_result(**patch)
    if kind == "integrity":
        return make_integrity_report(**patch)
    if kind == "delivery":
        integrity_status = patch.pop("integrity_status", None)
        reports = [make_integrity_report()]
        if integrity_status == "failed":
            reports = [make_integrity_report(status="failed", failures=["checksum mismatch"])]
        return DeliveryManifest(
            delivery_id="delivery-0001",
            request_id="req-era5-clwc-2011-2020",
            plan_id="plan-0001",
            dataset_metadata=make_metadata(),
            files=[make_file_record()],
            integrity=reports,
            created_at=datetime(2026, 11, 4, tzinfo=UTC),
            tool_version="0.1.0",
            **patch,
        )
    pytest.fail(f"未知 case kind: {kind}")


@pytest.mark.parametrize("case", RECORD_CASES, ids=[c["name"] for c in RECORD_CASES])
def test_invalid_record_inputs_rejected(case: dict[str, Any]) -> None:
    """资产/任务/传输/校验/交付层面的非法输入必须被拒绝。"""
    with pytest.raises(ValidationError) as excinfo:
        _build_invalid_record(case["kind"], dict(case["patch"]))
    message = str(excinfo.value)
    assert case["expect_field"] in message, (
        f"错误信息应指向 {case['expect_field']!r}，实际: {message}"
    )


# --------------------------------------------------------------------------- #
# 显式补充：类型错误与空值
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_size", [536870912.5, "53 MB", None, {"bytes": 1}])
def test_size_bytes_must_be_integer_bytes(bad_size: object) -> None:
    """文件大小必须是整数字节；Pydantic 的 lax 模式允许 "123" 这类数字字符串，
    但拒绝浮点数、带单位的字符串与 None。"""
    with pytest.raises(ValidationError):
        make_asset(size_bytes=bad_size)


def test_size_bytes_numeric_string_is_coerced_to_int() -> None:
    """lax 模式下 "123" 会被转成 123：这是 pydantic 的既定行为，此处固化语义，
    避免误以为所有字符串都会被拒。"""
    assert make_asset(size_bytes="123").size_bytes == 123


def test_request_id_must_be_lowercase_identifier() -> None:
    with pytest.raises(ValidationError, match="request_id"):
        make_request(request_id="REQ Whatever")


def test_priority_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError, match="priority"):
        make_request(priority="urgent")


def test_max_sources_upper_bound() -> None:
    with pytest.raises(ValidationError, match="max_sources"):
        make_request(max_sources=9)


def test_duplicate_integrity_checks_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        make_request(checks=["size_match", "size_match"])


def test_transfer_duration_cannot_be_negative() -> None:
    with pytest.raises(ValidationError, match="duration_seconds"):
        make_transfer_result(duration_seconds=-1.0)


def test_task_notes_must_be_strings() -> None:
    with pytest.raises(ValidationError):
        AcquisitionTask.model_validate({**make_task().model_dump(), "notes": [1, 2]})
