"""``rdh request`` / ``rdh plan`` / ``rdh task`` 的 CLI 行为测试（M4）。

覆盖范围
--------
CLI 层只做"参数 → service → 呈现"，因此这里断言的是**命令面契约**：
退出码、关键输出、缺少输入时明确失败、以及"DB 只读命令不需要数据源配置"。
调度执行细节由 ``tests/unit/test_scheduler`` 与 ``tests/integration/test_e2e_pipeline.py``
覆盖；真实外网的一次 plan/task run 已手工执行并记录在工程报告中。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import FIXED_NOW, make_request, make_task
from typer.testing import CliRunner

from researchdatahub.cli.main import app
from researchdatahub.core.models import AcquisitionPlan
from researchdatahub.core.state import TaskState
from researchdatahub.manifest.cli_service import open_manifest, resolve_db_target

REPO = Path(__file__).resolve().parents[2]
DEFAULTS = REPO / "configs" / "defaults.yaml"
EXAMPLES = REPO / "examples" / "era5-request.yaml"

runner = CliRunner()
WIDE_ENV = {"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"}

REQUEST_ID = "era5-clwc-ciwc-t-q-pl-2011-2021"
PLAN_ID = "plan-0001"


def invoke(*args: str) -> tuple[int, str]:
    """执行 CLI，返回 ``(退出码, 输出)``。"""
    result = runner.invoke(app, list(args), env=WIDE_ENV)
    return result.exit_code, result.output


def _prepare(tmp_path: Path) -> Path:
    """写一份 root 指向 tmp 的 defaults.yaml 并初始化数据库。"""
    root = tmp_path / "rdh"
    config = tmp_path / "defaults.yaml"
    config.write_text(
        DEFAULTS.read_text(encoding="utf-8").replace('root: "~/.rdh"', f'root: "{root}"'),
        encoding="utf-8",
    )
    code, output = invoke("db", "init", "--config", str(config))
    assert code == 0, output
    return config


def _write_sources(tmp_path: Path) -> Path:
    """写一份最小 sources.yaml（只含无凭证的 ncar 源），避免依赖 CDSAPI_KEY。"""
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        "sources:\n"
        "  ncar:\n"
        "    provider: NSF NCAR RDA\n"
        "    display_name: NCAR GDEX\n"
        "    adapter: rda\n"
        "    implemented: true\n"
        "    datasets:\n"
        "      - era5-pressure-levels\n"
        "    transports:\n"
        "      - route: https\n"
        "        adapter: https\n"
        "        implemented: true\n"
        "    endpoint: https://gdex.ucar.edu\n",
        encoding="utf-8",
    )
    return sources


def _seed_queued_task(config: Path, *, task_id: str = "task-0001") -> None:
    """播种一个 PLANNED→QUEUED 的任务（供 pause/resume/cancel/retry 使用）。"""
    repository = open_manifest(resolve_db_target(defaults_path=config))
    try:
        repository.save_request(make_request(request_id=REQUEST_ID))
        task = make_task(task_id=task_id, plan_id=PLAN_ID, request_id=REQUEST_ID)
        planned = task.model_copy(update={"state": TaskState.PLANNED})
        queued = planned.model_copy(update={"state": TaskState.QUEUED})
        repository.save_plan(
            AcquisitionPlan(
                plan_id=PLAN_ID,
                request_id=REQUEST_ID,
                tasks=[queued],
                generated_at=FIXED_NOW,
            ),
        )
    finally:
        repository.close()


# --------------------------------------------------------------------------- #
# request validate
# --------------------------------------------------------------------------- #
def test_request_validate_accepts_repository_example() -> None:
    """仓库自带示例必须能通过校验（否则示例与解析器已经漂移）。"""
    code, output = invoke("request", "validate", str(EXAMPLES))
    assert code == 0, output
    assert "需求校验通过" in output
    assert REQUEST_ID in output


def test_request_validate_missing_file_fails(tmp_path: Path) -> None:
    code, output = invoke("request", "validate", str(tmp_path / "nope.yaml"))
    assert code == 1, output
    assert "需求文件不存在" in output


def test_request_validate_reports_line_and_field(tmp_path: Path) -> None:
    """非法字段必须给出 ``文件:行`` 与字段路径，而不是笼统报错。"""
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        "request:\n  name: bad-request\n"
        "dataset:\n  provider: cds\n"
        "  name: reanalysis-era5-pressure-levels\n  product: reanalysis\n"
        "variables:\n  - name: t\n    units: K\n"
        "temporal:\n  start: '2011-01-01'\n  end: '2010-01-01'\n  frequency: hourly\n"
        "spatial:\n  type: global\n",
        encoding="utf-8",
    )
    code, output = invoke("request", "validate", str(broken))
    assert code == 1, output
    assert "错误" in output
    assert ".yaml:" in output or "行" in output


# --------------------------------------------------------------------------- #
# plan show / task status（只读命令不应要求数据源配置）
# --------------------------------------------------------------------------- #
def test_plan_show_missing_plan_fails(tmp_path: Path) -> None:
    config = _prepare(tmp_path)
    code, output = invoke("plan", "show", "plan-nope", "--config", str(config))
    assert code == 1, output
    assert "错误" in output


def test_plan_show_works_without_sources_config(tmp_path: Path) -> None:
    """关键回归：只读 DB 的命令不能因为"没配 CDS_KEY"而无法使用。"""
    config = _prepare(tmp_path)
    _seed_queued_task(config)
    code, output = invoke("plan", "show", PLAN_ID, "--config", str(config))
    assert code == 0, output
    assert PLAN_ID in output
    assert "task-0001" in output


def test_task_status_lists_seeded_task(tmp_path: Path) -> None:
    config = _prepare(tmp_path)
    _seed_queued_task(config)
    code, output = invoke("task", "status", "--config", str(config))
    assert code == 0, output
    assert "task-0001" in output
    assert "QUEUED" in output


def test_task_status_unknown_id_fails(tmp_path: Path) -> None:
    config = _prepare(tmp_path)
    _seed_queued_task(config)
    code, output = invoke("task", "status", "task-nope", "--config", str(config))
    assert code == 1, output
    assert "错误" in output


def test_task_status_detail_shows_lease_fields(tmp_path: Path) -> None:
    config = _prepare(tmp_path)
    _seed_queued_task(config)
    code, output = invoke("task", "status", "task-0001", "--config", str(config))
    assert code == 0, output
    for field in ("state", "attempt", "lease_expires_at", "heartbeat_at"):
        assert field in output


# --------------------------------------------------------------------------- #
# 控制类命令：pause / resume / cancel / retry（状态机校验）
# --------------------------------------------------------------------------- #
def test_task_pause_then_resume(tmp_path: Path) -> None:
    config = _prepare(tmp_path)
    _seed_queued_task(config)
    code, output = invoke("task", "pause", "task-0001", "--config", str(config))
    assert code == 0, output
    assert "PAUSED" in output
    code, output = invoke("task", "resume", "task-0001", "--config", str(config))
    assert code == 0, output
    assert "QUEUED" in output


def test_task_cancel_is_terminal(tmp_path: Path) -> None:
    config = _prepare(tmp_path)
    _seed_queued_task(config)
    code, output = invoke("task", "cancel", "task-0001", "--config", str(config))
    assert code == 0, output
    assert "CANCELLED" in output
    # 终态不可再暂停：必须是可读错误 + 退出码 1
    code, output = invoke("task", "pause", "task-0001", "--config", str(config))
    assert code == 1, output
    assert "错误" in output


def test_task_retry_requires_retry_wait(tmp_path: Path) -> None:
    """对 QUEUED 任务执行 retry 必须被状态机拒绝（RETRY_WAIT 才能 retry）。"""
    config = _prepare(tmp_path)
    _seed_queued_task(config)
    code, output = invoke("task", "retry", "task-0001", "--config", str(config))
    assert code == 1, output
    assert "错误" in output


def test_task_reconcile_on_empty_store_succeeds(tmp_path: Path) -> None:
    config = _prepare(tmp_path)
    code, output = invoke(
        "task",
        "reconcile",
        "--config",
        str(config),
        "--sources",
        str(_write_sources(tmp_path)),
        "--root",
        str(tmp_path / "rdh"),
    )
    assert code == 0, output
    assert "回收过期任务" in output


def test_task_run_without_plan_reports_zero(tmp_path: Path) -> None:
    """没有可执行任务时如实报告 0，而不是假装做了什么。"""
    config = _prepare(tmp_path)
    code, output = invoke(
        "task",
        "run",
        "--config",
        str(config),
        "--sources",
        str(_write_sources(tmp_path)),
        "--root",
        str(tmp_path / "rdh"),
    )
    assert code == 0, output
    assert "入队 0 个任务" in output


@pytest.mark.parametrize("command", ["request", "plan", "task"])
def test_command_group_help_lists_subcommands(command: str) -> None:
    code, output = invoke(command, "--help")
    assert code == 0, output
