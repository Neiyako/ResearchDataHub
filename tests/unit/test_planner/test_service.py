"""planner service 层测试：计划落库、读取、可渲染概览、需求校验 warning。

用真实 SQLite 临时库与真实 ``save_plan``/``load_plan``（不 mock 持久化语义）。
"""

from __future__ import annotations

import json

from tests.unit.test_planner.support import FakeAdapter, document, load_doc, make_asset, yaml_text

from researchdatahub.config.models import DefaultsConfig
from researchdatahub.planner.service import (
    create_plan,
    plan_overview,
    show_plan,
    validate_request_file,
)


def _request():
    request = load_doc(
        document(
            request={"name": "service_plan", "preferred_sources": ["ncar"], "max_sources": 1},
            variables=[{"name": "clwc", "units": "x"}],
        )
    ).request
    return request


def test_validate_request_file_returns_warnings(tmp_path):
    path = tmp_path / "request.yaml"
    path.write_text(yaml_text(document(variables=["clwc"])), encoding="utf-8")
    loaded = validate_request_file(str(path))
    assert loaded.request.request_id == "era5_cloud_water_2011"
    assert loaded.warnings
    payload = loaded.as_dict()
    assert payload["request_id"] == "era5_cloud_water_2011"
    json.dumps(payload)  # 必须 JSON 可序列化


def test_create_plan_persists_request_and_plan(repo):
    request = _request()
    adapter = FakeAdapter("ncar", assets=[make_asset(source_id="ncar", variables=["clwc"])])
    plan = create_plan(
        repo=repo,
        request=request,
        adapters=[adapter],
        config=DefaultsConfig(),
    )
    loaded_plan = show_plan(repo=repo, plan_id=plan.plan_id)
    assert loaded_plan.plan_id == plan.plan_id
    assert loaded_plan.task_ids() == plan.task_ids()
    assert repo.get_request(request.request_id).request_id == request.request_id


def test_plan_overview_is_json_safe(repo):
    request = _request()
    adapter = FakeAdapter("ncar", assets=[make_asset(source_id="ncar", variables=["clwc"])])
    plan = create_plan(repo=repo, request=request, adapters=[adapter], config=DefaultsConfig())
    overview = plan_overview(plan)
    assert overview["task_count"] == len(plan.tasks) == 1
    assert overview["tasks_by_source"] == {"ncar": 1}
    json.dumps(overview)
