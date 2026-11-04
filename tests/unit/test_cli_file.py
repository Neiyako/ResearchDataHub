"""``rdh file verify`` 的 CLI 行为测试。

断言的是**命令面契约**：退出码（PASSED=0，其余=1）、逐项检查可见性、
以及"没有期望值时不假装比对过"这条核心语义。
检查算法本身由 ``tests/unit/test_integrity/`` 覆盖，不在此重复。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from researchdatahub.cli.main import app

REPO = Path(__file__).resolve().parents[2]
DEFAULTS = REPO / "configs" / "defaults.yaml"

runner = CliRunner()
WIDE_ENV = {"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"}


def invoke(*args: str) -> tuple[int, str]:
    """执行 CLI，返回 ``(退出码, 输出)``。"""
    result = runner.invoke(app, list(args), env=WIDE_ENV)
    return result.exit_code, result.output


def _netcdf_available() -> bool:
    try:
        import netCDF4  # noqa: F401
    except ImportError:
        return False
    return True


def _write_netcdf(path: Path) -> Path:
    """写一个真实的小 NetCDF 文件（含变量、维度、单位属性）。"""
    import numpy as np
    from netCDF4 import Dataset

    with Dataset(path, "w", format="NETCDF4") as dataset:
        dataset.createDimension("time", 2)
        dataset.createDimension("lat", 3)
        dataset.createDimension("lon", 4)
        variable = dataset.createVariable("clwc", "f4", ("time", "lat", "lon"))
        variable.units = "kg kg**-1"
        variable.long_name = "Specific cloud liquid water content"
        variable[:] = np.zeros((2, 3, 4))
    return path


def test_file_verify_missing_path_fails_explicitly(tmp_path: Path) -> None:
    code, output = invoke("file", "verify", str(tmp_path / "nope.bin"))
    assert code == 1, output
    assert "错误" in output


def test_file_verify_without_expectations_is_not_passed(tmp_path: Path) -> None:
    """没有期望值时不得判 PASSED：size/checksum 检查只能记 skipped，但要有 sha256。"""
    target = tmp_path / "blob.bin"
    target.write_bytes(b"hello research data")
    code, output = invoke("file", "verify", str(target))
    assert code == 1, output
    assert "skipped" in output
    assert "不能据此认为文件已通过交付校验" in output
    assert hashlib.sha256(b"hello research data").hexdigest()[:8] in output


def test_file_verify_passes_when_size_and_sha256_match(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    payload = b"deterministic payload"
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    code, output = invoke(
        "file",
        "verify",
        str(target),
        "--expected-size",
        str(len(payload)),
        "--expected-sha256",
        digest,
        "--checks",
        "size_match",
        "--checks",
        "checksum_match",
    )
    assert code == 0, output
    assert "passed" in output


def test_file_verify_detects_checksum_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"payload")
    wrong = "0" * 64
    code, output = invoke(
        "file",
        "verify",
        str(target),
        "--expected-sha256",
        wrong,
        "--checks",
        "checksum_match",
    )
    assert code == 1, output
    assert "failed" in output


def test_file_verify_rejects_malformed_expected_sha256(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"payload")
    code, output = invoke("file", "verify", str(target), "--expected-sha256", "not-a-hash")
    assert code == 1, output
    assert "64 位十六进制" in output


def test_file_verify_json_output_is_stable(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"payload")
    code, output = invoke("file", "verify", str(target), "--json")
    assert code == 1, output  # skipped -> 非 0 退出
    assert '"status": "skipped"' in output
    # 不得出现伪造的上下文标识
    for forbidden in ("task_id", "file_id", "dataset_id"):
        assert f'"{forbidden}"' not in output


@pytest.mark.skipif(not _netcdf_available(), reason="netCDF4 未安装")
def test_file_verify_netcdf_openable_passes(tmp_path: Path) -> None:
    target = _write_netcdf(tmp_path / "sample.nc")
    code, output = invoke(
        "file",
        "verify",
        str(target),
        "--checks",
        "netcdf_openable",
        "--checks",
        "size_match",
        "--expected-size",
        str(target.stat().st_size),
    )
    assert code == 0, output
    assert "netCDF4" in output


@pytest.mark.skipif(not _netcdf_available(), reason="netCDF4 未安装")
def test_file_verify_detects_corrupt_netcdf(tmp_path: Path) -> None:
    target = tmp_path / "broken.nc"
    # 真实损坏样本：把合法 NetCDF 截断到 1/3。注意"魔数 + 全零"其实可以被
    # netCDF4 当作空 NETCDF3_CLASSIC 打开——那不算损坏，用它做样本会得到假结论。
    whole = _write_netcdf(tmp_path / "whole.nc").read_bytes()
    target.write_bytes(whole[: len(whole) // 3])
    code, output = invoke("file", "verify", str(target), "--checks", "netcdf_openable")
    assert code == 1, output
    assert "failed" in output


def test_file_verify_respects_documented_checks_in_help() -> None:
    code, output = invoke("file", "verify", "--help")
    assert code == 0, output
    assert "--expected-size" in output
    assert re.search("PASSED|完整性", output)
