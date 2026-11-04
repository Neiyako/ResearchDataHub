"""``rdh report generate|show`` 的 CLI 行为测试（M5）。

使用真实临时 SQLite + 真实临时存储目录，通过 Manifest Repository 播种事实，
再跑 CLI 断言"报告数字来自数据库、不是猜的"。

不写 ``~/.rdh``：配置文件里的 ``storage.root`` 被重写为 ``tmp_path``。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from tests.conftest import (
    FIXED_NOW,
    make_asset,
    make_file_record,
    make_integrity_report,
    make_request,
    make_task,
)
from typer.testing import CliRunner

from researchdatahub.cli.main import app
from researchdatahub.core.models import AcquisitionPlan
from researchdatahub.core.state import TaskState, transition

REPO = Path(__file__).resolve().parents[2]
DEFAULTS = REPO / "configs" / "defaults.yaml"

runner = CliRunner()
WIDE_ENV = {"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"}

REQUEST_ID = "era5-clwc-ciwc-t-q-pl-2011-2021"
PLAN_ID = "plan-0001"


def invoke(*args: str) -> tuple[int, str]:
    """执行 CLI，返回 ``(退出码, 输出)``。"""
    result = runner.invoke(app, list(args), env=WIDE_ENV)
    return result.exit_code, result.output


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    """在临时目录写一份 defaults.yaml（root 指向 tmp）并初始化数据库。"""
    root = tmp_path / "rdh"
    config = tmp_path / "defaults.yaml"
    config.write_text(
        DEFAULTS.read_text(encoding="utf-8").replace('root: "~/.rdh"', f'root: "{root}"'),
        encoding="utf-8",
    )
    code, output = invoke("db", "init", "--config", str(config))
    assert code == 0, output
    return config, root


def _seed(tmp_path: Path, config: Path) -> None:
    """播种一个已完成的采集：请求 + 计划 + COMPLETED 任务 + 文件 + PASSED 报告。"""
    from researchdatahub.manifest.cli_service import open_manifest, resolve_db_target

    repository = open_manifest(resolve_db_target(defaults_path=config))
    try:
        request = make_request(request_id=REQUEST_ID)
        repository.save_request(request)
        asset = make_asset()
        task = make_task(
            task_id="task-0001",
            plan_id=PLAN_ID,
            request_id=REQUEST_ID,
            asset=asset,
            state=TaskState.QUEUED,
            max_attempts=3,
        )
        completed = transition(task, TaskState.RUNNING, reason="claim", now=FIXED_NOW)
        completed = transition(
            completed, TaskState.VERIFYING, reason="transfer done", now=FIXED_NOW
        )
        completed = transition(completed, TaskState.COMPLETED, reason="verified", now=FIXED_NOW)
        repository.save_plan(
            AcquisitionPlan(
                plan_id=PLAN_ID,
                request_id=REQUEST_ID,
                tasks=[completed],
                generated_at=FIXED_NOW,
            ),
        )
        record = make_file_record(task_id="task-0001")
        repository.save_file_record(record)
        repository.save_integrity_report(
            make_integrity_report(task_id="task-0001", file_id=record.file_id),
        )
        repository.upsert_source(_source_metadata(), adapter_id="rda")
    finally:
        repository.close()


def _source_metadata():
    from tests.conftest import make_source_metadata

    return make_source_metadata()


def test_report_generate_without_tasks_fails(tmp_path: Path) -> None:
    config, _root = _prepare(tmp_path)
    code, output = invoke("report", "generate", REQUEST_ID, "--config", str(config))
    assert code == 1, output
    assert "错误" in output


def test_report_generate_writes_report_and_delivery(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path)
    _seed(tmp_path, config)

    code, output = invoke("report", "generate", REQUEST_ID, "--config", str(config))
    assert code == 0, output
    assert "报告：" in output

    reports = sorted((root / "reports").glob("report-*.json"))
    assert len(reports) == 1, reports
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["request_id"] == REQUEST_ID
    assert payload["counts"]["completed"] == 1
    assert payload["integrity"]["passed"] == 1
    assert payload["total_bytes"] > 0
    # 交付清单也应生成（文件校验 PASSED）
    assert payload["delivery_manifest_path"] is not None
    assert (root / "deliveries").is_dir()


def test_report_show_renders_existing_report(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path)
    _seed(tmp_path, config)
    assert invoke("report", "generate", REQUEST_ID, "--config", str(config))[0] == 0
    report_id = sorted((root / "reports").glob("*.json"))[0].stem

    code, output = invoke("report", "show", report_id, "--config", str(config))
    assert code == 0, output
    assert REQUEST_ID in output
    assert "校验" in output


def test_report_show_missing_report_fails(tmp_path: Path) -> None:
    config, _root = _prepare(tmp_path)
    code, output = invoke("report", "show", "report-missing", "--config", str(config))
    assert code == 1, output
    assert "报告不存在" in output


def test_report_show_json_mode(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path)
    _seed(tmp_path, config)
    assert invoke("report", "generate", REQUEST_ID, "--config", str(config))[0] == 0
    report_id = sorted((root / "reports").glob("*.json"))[0].stem
    code, output = invoke("report", "show", report_id, "--config", str(config), "--json")
    assert code == 0, output
    assert '"request_id"' in output


def test_report_generate_reports_failed_tasks_not_only_successes(tmp_path: Path) -> None:
    """失败任务必须出现在报告里：只报成功数量的报告视为缺陷。"""
    from researchdatahub.manifest.cli_service import open_manifest, resolve_db_target

    config, root = _prepare(tmp_path)
    repository = open_manifest(resolve_db_target(defaults_path=config))
    try:
        repository.save_request(make_request(request_id=REQUEST_ID))
        ok_task = transition(
            transition(
                transition(
                    make_task(task_id="task-0001", plan_id=PLAN_ID, request_id=REQUEST_ID),
                    TaskState.PLANNED,
                    reason="planned",
                    now=FIXED_NOW,
                ),
                TaskState.QUEUED,
                reason="queued",
                now=FIXED_NOW,
            ),
            TaskState.CANCELLED,
            reason="user cancelled",
            now=FIXED_NOW,
        )
        failed = transition(
            transition(
                transition(
                    transition(
                        make_task(task_id="task-0002", plan_id=PLAN_ID, request_id=REQUEST_ID),
                        TaskState.PLANNED,
                        reason="planned",
                        now=FIXED_NOW,
                    ),
                    TaskState.QUEUED,
                    reason="queued",
                    now=FIXED_NOW,
                ),
                TaskState.RUNNING,
                reason="claimed",
                now=FIXED_NOW,
            ),
            TaskState.FAILED,
            reason="http 404",
            error="permanent: not found",
            now=FIXED_NOW,
        )
        repository.save_plan(
            AcquisitionPlan(
                plan_id=PLAN_ID,
                request_id=REQUEST_ID,
                tasks=[ok_task, failed],
                generated_at=FIXED_NOW,
            ),
        )
    finally:
        repository.close()

    code, output = invoke("report", "generate", REQUEST_ID, "--config", str(config))
    assert code == 0, output
    payload = json.loads(
        sorted((root / "reports").glob("*.json"))[0].read_text(encoding="utf-8"),
    )
    assert payload["counts"]["failed"] == 1
    assert payload["counts"]["skipped"] == 0
    assert payload["failures"], "失败任务必须留下失败记录"
    assert payload["failures"][0]["task_id"] == "task-0002"


def test_report_generate_respects_plan_option(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path)
    _seed(tmp_path, config)
    code, output = invoke(
        "report", "generate", REQUEST_ID, "--config", str(config), "--plan", PLAN_ID
    )
    assert code == 0, output
    payload = json.loads(sorted((root / "reports").glob("*.json"))[0].read_text(encoding="utf-8"))
    assert payload["plan_id"] == PLAN_ID


def test_report_generated_at_uses_utc_now(tmp_path: Path) -> None:
    config, root = _prepare(tmp_path)
    _seed(tmp_path, config)
    assert invoke("report", "generate", REQUEST_ID, "--config", str(config))[0] == 0
    payload = json.loads(sorted((root / "reports").glob("*.json"))[0].read_text(encoding="utf-8"))
    generated = datetime.fromisoformat(payload["generated_at"])
    assert generated.tzinfo is not None
    assert generated.utcoffset() == UTC.utcoffset(None)


def test_show_help_lists_commands() -> None:
    code, output = invoke("report", "--help")
    assert code == 0, output
    assert "generate" in output
    assert "show" in output
