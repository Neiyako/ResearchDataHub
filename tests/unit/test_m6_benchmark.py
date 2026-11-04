"""M6 效率基准脚本的离线单元测试（不联网）。

被测文件是 ``scripts/m6_efficiency_bench.py``（不是包内模块，因此用 importlib 按路径加载）。
只测**纯函数**：请求构造、GDEX 文件清单选择、汇总与渲染。真实下载一律不进默认套件。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "m6_efficiency_bench.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("m6_efficiency_bench", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["m6_efficiency_bench"] = module
    spec.loader.exec_module(module)
    return module


def _filelist_payload(rows: list[dict[str, object]]) -> dict[str, object]:
    return {"data": {"groups": [{"rows": rows}]}}


def _row(name: str, size: int, url: str) -> list[dict[str, object]]:
    return [
        {"name": "File Name", "value": name, "url": url},
        {"name": "Size", "value": size},
        {"name": "Data Format", "value": "netCDF4"},
    ]


def test_build_cds_request_uses_official_cds_parameter_names(bench):
    request = bench.build_cds_request(
        year=2024, month=1, days=[1, 2, 3], hours=["00:00", "12:00"], area=[45, 100, 30, 125]
    )
    assert request["product_type"] == "reanalysis"
    assert request["variable"] == ["2m_temperature"]
    assert request["year"] == "2024"
    assert request["month"] == "01"
    assert request["day"] == ["01", "02", "03"]
    assert request["time"] == ["00:00", "12:00"]
    assert request["area"] == [45.0, 100.0, 30.0, 125.0]
    assert request["data_format"] == "netcdf"


def test_select_ncar_file_picks_month_and_variable(bench):
    payload = _filelist_payload(
        [
            _row("e5.oper.an.sfc.128_167_2t.ll025sc.2024010100_2024013123.nc", 1134576716, "u1"),
            _row("e5.oper.an.sfc.128_167_2t.ll025sc.2024020100_2024022923.nc", 1062915605, "u2"),
            _row("e5.oper.an.sfc.128_134_sp.ll025sc.2024010100_2024013123.nc", 500, "u3"),
        ]
    )
    entry = bench.select_ncar_file(payload, var_code="128_167_2t", year=2024, month=1)
    assert entry is not None
    assert entry["file_name"].endswith("2024010100_2024013123.nc")
    assert entry["size_bytes"] == 1134576716
    assert entry["url"] == "u1"


def test_select_ncar_file_returns_none_when_month_missing(bench):
    payload = _filelist_payload(
        [_row("e5.oper.an.sfc.128_167_2t.ll025sc.2024010100_2024013123.nc", 10, "u1")]
    )
    assert bench.select_ncar_file(payload, var_code="128_167_2t", year=2024, month=2) is None
    assert bench.select_ncar_file({"data": {}}, var_code="128_167_2t", year=2024, month=1) is None


def test_select_ncar_file_tolerates_malformed_rows(bench):
    payload = _filelist_payload(
        [
            {"name": "File Name", "value": 42},
            {"name": "Size", "value": "not-a-number"},
        ]
    )
    assert bench.select_ncar_file(payload, var_code="128_167_2t", year=2024, month=1) is None


def test_build_summary_computes_effective_throughput(bench):
    record = bench.RunRecord(mode="cds", workload={})
    record.ok = True
    record.wall_seconds = 60.0
    record.bytes_total = 6_000_000
    row = bench.build_summary([record])[0]
    assert row["mode"] == "cds"
    assert row["ok"] is True
    assert row["wall_seconds"] == 60.0
    assert row["effective_mbps"] == 0.1


def test_build_summary_marks_unknown_throughput_when_nothing_downloaded(bench):
    record = bench.RunRecord(mode="cds", workload={})
    row = bench.build_summary([record])[0]
    assert row["effective_mbps"] is None


def test_build_summary_exposes_mixed_hedge_fields(bench):
    record = bench.RunRecord(mode="mixed", workload={})
    record.ok = True
    record.wall_seconds = 90.0
    record.bytes_total = 1_200_000_000
    record.validation = {
        "winner": "cds",
        "time_to_first_artifact_seconds": 61.5,
        "wasted_bytes": 1_100_000_000,
    }
    row = bench.build_summary([record])[0]
    assert row["winner"] == "cds"
    assert row["time_to_first_artifact_seconds"] == 61.5
    assert row["wasted_bytes"] == 1_100_000_000


def test_render_markdown_contains_result_table(bench):
    record = bench.RunRecord(mode="ncar", workload={})
    record.ok = True
    record.wall_seconds = 10.0
    record.bytes_total = 16_000_000
    record.phases = {"catalog": 2.0, "download": 8.0}
    markdown = bench.render_markdown(
        [record], {"generated_at": "2026-11-04", "environment": "test"}
    )
    assert "| 条件 | 成功 | 端到端耗时 (s) |" in markdown
    assert "| ncar | PASS | 10.0 | 16000000 |" in markdown
    assert "catalog=2.0" in markdown


def test_render_markdown_reports_mixed_hedge_line(bench):
    record = bench.RunRecord(mode="mixed", workload={})
    record.ok = True
    record.wall_seconds = 95.0
    record.bytes_total = 1_200_000_000
    record.validation = {
        "winner": "ncar",
        "time_to_first_artifact_seconds": 80.0,
        "wasted_bytes": 5_000_000,
    }
    markdown = bench.render_markdown([record], {"generated_at": "x", "environment": "y"})
    assert "混合制先就绪：80.0 s" in markdown
    assert "winner=ncar" in markdown


def test_choose_hedge_winner_ignores_failed_fast_side(bench):
    """失败的一路即使 wall≈0 也不能当赢家（CDS 被队列上限拒绝时的真实场景）。"""
    winner, seconds = bench.choose_hedge_winner(
        {"cds": 0.0, "ncar": 113.6}, {"cds": False, "ncar": True}
    )
    assert winner == "ncar"
    assert seconds == 113.6


def test_choose_hedge_winner_picks_fastest_successful_side(bench):
    winner, seconds = bench.choose_hedge_winner(
        {"cds": 61.5, "ncar": 113.6}, {"cds": True, "ncar": True}
    )
    assert winner == "cds"
    assert seconds == 61.5


def test_choose_hedge_winner_returns_none_when_all_failed(bench):
    assert bench.choose_hedge_winner({"cds": 1.0}, {"cds": False}) == (None, None)
