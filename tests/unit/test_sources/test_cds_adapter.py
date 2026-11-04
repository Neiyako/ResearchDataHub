"""CDS 适配器与异步生命周期测试（真实本地 HTTP 服务器 + 真实官方客户端）。

这里**不 mock** 官方 ``ecmwf-datastores-client``：本地服务器实现 CDS retrieve/v1 的
真实协议形状（process → execution → monitor → results），客户端是安装好的官方实现。
"""

from __future__ import annotations

import logging

import httpx
import pytest
from tests.conftest import make_request
from tests.unit.test_sources.conftest import (
    CapturedRequest,
    LocalHttpServer,
    Response,
    StepClock,
    load_fixture,
    make_source_config,
)

from researchdatahub.core.errors import SourceUnavailableError
from researchdatahub.core.models import AccessRoute
from researchdatahub.sources.cds import (
    CDSAdapter,
    CdsJobState,
    CdsRequestLifecycle,
    EcmwfDatastoresBridge,
    probe_cds_dependencies,
)

API = "/api"
COLLECTION = "reanalysis-era5-pressure-levels"
PROCESS_PATH = f"{API}/retrieve/v1/processes/{COLLECTION}"
MONITOR_PATH = f"{API}/retrieve/v1/jobs/job-1"
RESULTS_PATH = f"{MONITOR_PATH}/results"
CHECKSUM = "sha256:" + "a" * 64
RESULT_SIZE = 12345


def install_cds_routes(
    server: LocalHttpServer,
    *,
    statuses: tuple[str, ...] = ("accepted", "running", "successful"),
    checksum: object = CHECKSUM,
    result_size: int = RESULT_SIZE,
    execution_status: int = 200,
) -> None:
    """在本地服务器上实现 CDS retrieve/v1 的真实交互形状。"""
    base = server.url
    process_payload = load_fixture("cds_process_era5_pressure_levels.json")
    server.route("GET", PROCESS_PATH, lambda _request: Response.json(process_payload))
    server.add_json("GET", f"{API}/catalogue/v1/messages", {"messages": []})
    state = {"index": 0}

    def monitor(_request: CapturedRequest) -> Response:
        index = min(state["index"], len(statuses) - 1)
        state["index"] += 1
        status = statuses[index]
        links = []
        if status == "successful":
            links = [{"rel": "results", "href": f"{base}{RESULTS_PATH}"}]
        return Response.json(
            {
                "status": status,
                "links": links,
                "created": "2026-11-04T10:00:00.000000",
                "updated": "2026-11-04T10:01:00.000000",
            }
        )

    server.route("GET", MONITOR_PATH, monitor)
    server.add_json(
        "POST",
        f"{PROCESS_PATH}/execution",
        {
            "status": "accepted",
            "links": [{"rel": "monitor", "href": f"{base}{MONITOR_PATH}"}],
        },
        status=execution_status,
    )

    def results(_request: CapturedRequest) -> Response:
        return Response.json(
            {
                "asset": {
                    "value": {
                        "type": "application/zip",
                        "href": f"{base}{API}/download/result.zip",
                        "file:checksum": checksum,
                        "file:size": result_size,
                        "file:local_path": "result.zip",
                    }
                }
            }
        )

    server.route("GET", RESULTS_PATH, results)
    server.add("GET", f"{API}/download/result.zip", Response(body=b"binary-bytes"))


def _bridge(server: LocalHttpServer) -> EcmwfDatastoresBridge:
    return EcmwfDatastoresBridge(
        api_root=f"{server.url}{API}",
        key="test-key",
        dependencies=probe_cds_dependencies(),
        timeout=2.0,
        maximum_tries=1,
        retry_after=0.01,
    )


def _adapter(server: LocalHttpServer, **kwargs) -> CDSAdapter:
    return CDSAdapter(
        source_id="cds",
        api_root=f"{server.url}{API}",
        credentials={"url": f"{server.url}{API}", "key": "test-key"},
        dependency_probe=probe_cds_dependencies,
        request_timeout_seconds=5.0,
        sleep=lambda _seconds: None,
        http_client=httpx.Client(timeout=2.0, follow_redirects=True),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #
def test_lifecycle_submit_poll_wait_results(http_server: LocalHttpServer):
    """提交 → 轮询 accepted/running → successful → 结果元数据（临时 URL/大小/类型）。"""
    install_cds_routes(http_server)
    lifecycle = CdsRequestLifecycle(_bridge(http_server), sleep=lambda _seconds: None)
    submission = lifecycle.submit(COLLECTION, {"variable": ["temperature"]})
    snapshot = lifecycle.poll(submission)
    assert snapshot.state is CdsJobState.ACCEPTED
    assert snapshot.request_id == "job-1"
    final = lifecycle.wait(submission, timeout_seconds=5.0, initial_interval=0.01)
    assert final.state is CdsJobState.SUCCESSFUL
    result = lifecycle.results(submission)
    assert result.location is not None
    assert result.location.endswith("/download/result.zip")
    assert result.size_bytes == RESULT_SIZE
    assert result.content_type == "application/zip"
    assert result.checksum_raw == CHECKSUM
    # 发现阶段只取元数据，绝不下载字节。
    assert http_server.requests_to(f"{API}/download/result.zip") == ()
    submitted = http_server.requests_to(f"{PROCESS_PATH}/execution")
    assert len(submitted) == 1
    assert submitted[0].json_body()["inputs"] == {"variable": ["temperature"]}


def test_lifecycle_failed_status_raises_with_remote_state(http_server: LocalHttpServer):
    """失败终态折算为 SourceUnavailableError，并带远端状态。"""
    install_cds_routes(http_server, statuses=("accepted", "failed"))
    lifecycle = CdsRequestLifecycle(_bridge(http_server), sleep=lambda _seconds: None)
    submission = lifecycle.submit(COLLECTION, {"variable": ["temperature"]})
    with pytest.raises(SourceUnavailableError) as caught:
        lifecycle.wait(submission, timeout_seconds=5.0, initial_interval=0.01)
    assert caught.value.detail["remote_status"] == "failed"


def test_lifecycle_unknown_status_is_not_treated_as_success(http_server: LocalHttpServer):
    """未知状态不得当成成功，也不能无限等下去。"""
    install_cds_routes(http_server, statuses=("weird",))
    lifecycle = CdsRequestLifecycle(_bridge(http_server), sleep=lambda _seconds: None)
    submission = lifecycle.submit(COLLECTION, {"variable": ["temperature"]})
    with pytest.raises(SourceUnavailableError):
        lifecycle.wait(submission, timeout_seconds=5.0, initial_interval=0.01)


def test_lifecycle_timeout_is_reported_not_swallowed(http_server: LocalHttpServer):
    """轮询超时抛错，并记录 last_status / polls（用固定时钟，不依赖真实等待）。"""
    install_cds_routes(http_server, statuses=("running",))
    clock = StepClock(step_seconds=10.0)
    lifecycle = CdsRequestLifecycle(_bridge(http_server), clock=clock, sleep=lambda _seconds: None)
    submission = lifecycle.submit(COLLECTION, {"variable": ["temperature"]})
    with pytest.raises(SourceUnavailableError) as caught:
        lifecycle.wait(submission, timeout_seconds=5.0, initial_interval=1.0)
    assert caught.value.detail["last_status"] == "running"
    assert caught.value.detail["timeout_seconds"] == 5.0


def test_bridge_maps_connection_failure_to_source_error():
    """真实 connection refused（服务器已停止）→ SourceUnavailableError。"""
    server = LocalHttpServer()
    api_root = f"{server.url}{API}"
    server.stop()
    bridge = EcmwfDatastoresBridge(
        api_root=api_root,
        key="test-key",
        dependencies=probe_cds_dependencies(),
        timeout=0.5,
        maximum_tries=1,
        retry_after=0.01,
    )
    with pytest.raises(SourceUnavailableError):
        bridge.submit(COLLECTION, {"variable": ["temperature"]})


def test_bridge_without_client_dependency_reports_unavailable(monkeypatch):
    """依赖缺失时明确 unavailable（不伪造客户端）。"""
    import researchdatahub.sources.cds as cds_module

    monkeypatch.setattr(cds_module, "probe_cds_dependencies", lambda: None)
    bridge = EcmwfDatastoresBridge(api_root="https://example.invalid/api", key="test-key")
    with pytest.raises(SourceUnavailableError) as caught:
        bridge.submit(COLLECTION, {"variable": ["temperature"]})
    assert "not installed" in caught.value.message


# --------------------------------------------------------------------------- #
# 适配器：describe / health_check
# --------------------------------------------------------------------------- #
def test_describe_reads_real_shaped_catalogue_metadata(http_server: LocalHttpServer):
    """describe 使用真实形状的 catalogue 响应，license/题名进入 SourceMetadata。"""
    catalogue = load_fixture("cds_catalogue_era5_pressure_levels.json")
    http_server.add_json("GET", f"{API}/catalogue/v1/collections/{COLLECTION}", catalogue)
    http_server.add_json("GET", f"{API}/catalogue/v1/messages", {"messages": []})
    adapter = _adapter(http_server, config=make_source_config(adapter="cds", endpoint=None))
    metadata = adapter.describe()
    assert metadata.source_id == "cds"
    assert metadata.license == "CC-BY-4.0"
    assert metadata.access_route is AccessRoute.CDS_API
    assert metadata.protocol_version == "retrieve/v1"
    assert metadata.dataset_id == COLLECTION
    assert all("." in key for key in metadata.extensions)
    assert metadata.extensions["cds.collection_id"] == COLLECTION
    assert metadata.extensions["cds.area_order_verified"] is True


def test_health_check_true_on_ok_and_false_on_http_error(http_server: LocalHttpServer):
    """健康检查 200 → True；500 → False（不抛异常）。"""
    http_server.add_json("GET", PROCESS_PATH, load_fixture("cds_process_era5_pressure_levels.json"))
    adapter = _adapter(http_server)
    assert adapter.health_check() is True

    error_server = LocalHttpServer()
    try:
        error_server.add_json("GET", PROCESS_PATH, {"detail": "boom"}, status=500)
        error_adapter = _adapter(error_server)
        assert error_adapter.health_check() is False
    finally:
        error_server.stop()


def test_health_check_false_on_real_connection_failure():
    """真实 connection refused → health_check 返回 False（协议要求不抛异常）。"""
    server = LocalHttpServer()
    api_root = f"{server.url}{API}"
    server.stop()
    adapter = CDSAdapter(
        source_id="cds",
        api_root=api_root,
        credentials={"url": api_root, "key": "test-key"},
        dependency_probe=probe_cds_dependencies,
        http_client=httpx.Client(timeout=0.5),
    )
    assert adapter.health_check() is False


def test_schema_fetch_timeout_becomes_source_unavailable(http_server: LocalHttpServer):
    """真实读超时（服务器延迟响应）→ SourceUnavailableError。"""
    http_server.add(
        "GET",
        PROCESS_PATH,
        Response.json(load_fixture("cds_process_era5_pressure_levels.json"), delay_seconds=0.5),
    )
    adapter = CDSAdapter(
        source_id="cds",
        api_root=f"{http_server.url}{API}",
        credentials={"url": f"{http_server.url}{API}", "key": "test-key"},
        dependency_probe=probe_cds_dependencies,
        timeout=0.1,
        http_client=httpx.Client(timeout=0.1),
    )
    with pytest.raises(SourceUnavailableError):
        adapter.fetch_process_schema(COLLECTION)


# --------------------------------------------------------------------------- #
# 适配器：discover_assets 端到端
# --------------------------------------------------------------------------- #
def test_discover_assets_end_to_end_produces_source_asset(http_server: LocalHttpServer):
    """完整发现链路：schema → 翻译 → 提交 → 轮询 → 结果 URL → SourceAsset。"""
    install_cds_routes(http_server)
    adapter = _adapter(http_server)
    assets = adapter.discover_assets(make_request())
    assert len(assets) == 1
    asset = assets[0]
    assert asset.source_id == "cds"
    assert asset.dataset_id == "era5-pressure-levels"
    assert asset.access_route is AccessRoute.CDS_API
    assert asset.origin_uri.endswith("/api/download/result.zip")
    assert asset.file_name == "result.zip"
    assert asset.size_bytes == RESULT_SIZE
    assert asset.content_type == "application/zip"
    assert asset.checksum is not None
    assert asset.checksum.algorithm == "sha256"
    assert asset.metadata.origin_uri == asset.origin_uri
    assert asset.metadata.source_id == asset.source_id
    assert asset.time_slice is not None
    assert asset.metadata.extensions["cds.request_id"] == "job-1"
    assert asset.metadata.extensions["cds.collection_id"] == COLLECTION
    assert asset.metadata.extensions["cds.result_location_is_temporary"] is True
    translated = asset.metadata.extensions["cds.request"]
    assert isinstance(translated, dict)
    assert translated["data_format"] == "netcdf"
    # 发现阶段绝不下载字节。
    assert http_server.requests_to(f"{API}/download/result.zip") == ()


def test_discover_keeps_unrecognized_checksum_raw_without_guessing(
    http_server: LocalHttpServer,
):
    """无法识别的远端校验和格式：checksum=None，原文进 extensions 并标记未识别。"""
    install_cds_routes(http_server, checksum="deadbeef")
    adapter = _adapter(http_server)
    asset = adapter.discover_assets(make_request())[0]
    assert asset.checksum is None
    assert asset.metadata.extensions["cds.result_checksum_raw"] == "deadbeef"
    assert asset.metadata.extensions["cds.result_checksum_recognized"] is False


def test_discover_requires_dependencies_and_credentials(http_server: LocalHttpServer):
    """缺依赖/缺凭证时在提交前明确失败（不生成假资产、不发 POST）。"""
    install_cds_routes(http_server)
    no_deps = CDSAdapter(
        source_id="cds",
        api_root=f"{http_server.url}{API}",
        credentials={"url": f"{http_server.url}{API}", "key": "test-key"},
        dependency_probe=lambda: None,
        sleep=lambda _seconds: None,
        http_client=httpx.Client(timeout=2.0),
    )
    with pytest.raises(SourceUnavailableError):
        no_deps.discover_assets(make_request())
    assert http_server.requests_to(f"{PROCESS_PATH}/execution") == ()

    no_credentials = CDSAdapter(
        source_id="cds",
        api_root=f"{http_server.url}{API}",
        credentials={},
        dependency_probe=probe_cds_dependencies,
        sleep=lambda _seconds: None,
        http_client=httpx.Client(timeout=2.0),
    )
    with pytest.raises(SourceUnavailableError):
        no_credentials.discover_assets(make_request())
    assert http_server.requests_to(f"{PROCESS_PATH}/execution") == ()


def test_discover_failure_maps_to_source_unavailable(http_server: LocalHttpServer):
    """远端任务失败时 discovery 抛 SourceUnavailableError（属于发现阶段错误）。"""
    install_cds_routes(http_server, statuses=("accepted", "failed"))
    adapter = _adapter(http_server)
    with pytest.raises(SourceUnavailableError):
        adapter.discover_assets(make_request())


def test_secrets_are_never_logged_or_raised(http_server: LocalHttpServer, caplog):
    """凭证只允许以键名出现：日志/异常/能力描述里不得出现 key 的值。"""
    secret = "super-secret-cds-token-value"
    install_cds_routes(http_server, execution_status=500)
    adapter = CDSAdapter(
        source_id="cds",
        api_root=f"{http_server.url}{API}",
        credentials={"url": f"{http_server.url}{API}", "key": secret},
        dependency_probe=probe_cds_dependencies,
        request_timeout_seconds=2.0,
        sleep=lambda _seconds: None,
        http_client=httpx.Client(timeout=2.0),
    )
    with caplog.at_level(logging.DEBUG):
        assert adapter.health_check() is True
        with pytest.raises(SourceUnavailableError) as caught:
            adapter.discover_assets(make_request())
        rendered = str(caught.value) + str(caught.value.detail) + caplog.text
        assert secret not in rendered
        assert secret not in str(adapter.capabilities().to_dict())
