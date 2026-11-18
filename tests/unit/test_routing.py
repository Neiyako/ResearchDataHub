"""Route Intelligence：能力五态、探活/资格、选择与偷懒路径回归。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.conftest import make_asset, make_source_metadata, make_task

from researchdatahub.config.models import (
    PlannerConfig,
    SourceConfig,
    SourcesConfig,
    TransportBinding,
)
from researchdatahub.core.models import AccessRoute, AcquisitionPlan, DatasetRequest, SourceAsset
from researchdatahub.network.routes.models import RouteQualification
from researchdatahub.planner.planner import DefaultTaskPlanner
from researchdatahub.routing import (
    AdapterImplementationStatus,
    BenchmarkResult,
    CheckStatus,
    NoQualifiedRouteError,
    ProbeResult,
    Route,
    RouteCapabilities,
    RouteExperienceRegistry,
    RouteIntelligence,
    RouteStatus,
    WorkloadTier,
    aggregate_checks,
)
from researchdatahub.routing.capabilities import check_capabilities
from researchdatahub.routing.errors import RouteExecutionError
from researchdatahub.routing.models import RouteHistory

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def load_era5_requirement() -> DatasetRequest:
    payload = json.loads(
        Path("tests/fixtures/era5_route_planning.json").read_text(encoding="utf-8")
    )
    return DatasetRequest.model_validate(payload)


def all_pass_capabilities(request: DatasetRequest) -> RouteCapabilities:
    return RouteCapabilities(
        dataset=CheckStatus.PASS,
        variables=dict.fromkeys(request.required_variables(), CheckStatus.PASS),
        temporal_coverage=CheckStatus.PASS,
        temporal_resolution=CheckStatus.PASS,
        spatial_coverage=CheckStatus.PASS,
        spatial_resolution=CheckStatus.PASS,
        vertical_levels=CheckStatus.PASS,
        format=CheckStatus.PASS,
        subset=CheckStatus.PASS,
        authentication=CheckStatus.PASS,
        transport=CheckStatus.PASS,
        formats=["netcdf"],
        temporal_resolutions=["utc_hourly"],
        spatial_resolutions=[0.25],
        subset_supported=True,
    )


def benchmark(throughput_mib_s: float, *, latency_ms: float = 100.0) -> BenchmarkResult:
    size = int(throughput_mib_s * 1024**2 * 2)
    return BenchmarkResult(
        probe_size=size,
        latency_ms=latency_ms,
        transfer_time_s=2.0,
        throughput_bps=throughput_mib_s * 1024**2,
        success=True,
        timestamp=NOW,
    )


def route(
    route_id: str,
    *,
    source: str,
    transport: str,
    request: DatasetRequest,
    qualification: RouteQualification = RouteQualification.DISCOVERED,
    status: RouteStatus = RouteStatus.UNKNOWN,
    adapter_status: AdapterImplementationStatus = AdapterImplementationStatus.IMPLEMENTED,
    capabilities: RouteCapabilities | None = None,
    bench: BenchmarkResult | None = None,
    last_verified: datetime | None = NOW,
) -> Route:
    return Route(
        id=route_id,
        source=source,
        dataset=request.dataset_id,
        transport=transport,
        adapter=source,
        endpoint=f"https://{source}.example.test",
        capabilities=capabilities or all_pass_capabilities(request),
        adapter_status=adapter_status,
        status=status,
        qualification=qualification,
        benchmark=bench,
        last_verified=last_verified,
        history=RouteHistory(
            last_verified=last_verified,
            benchmark=bench,
            attempts=4 if bench else 0,
            successes=4 if bench else 0,
            success_rate=1.0 if bench else None,
        ),
    )


class RecordingProbe:
    def __init__(self, status: CheckStatus = CheckStatus.UNKNOWN) -> None:
        self.status = status
        self.calls: list[str] = []

    def probe(self, route: Route, request: DatasetRequest, adapter: object) -> ProbeResult:
        self.calls.append(route.id)
        return ProbeResult(
            route_id=route.id,
            status=self.status,
            endpoint_ok=self.status is CheckStatus.PASS,
            response_valid=self.status is CheckStatus.PASS,
            capabilities=route.capabilities if self.status is CheckStatus.PASS else None,
            checked_at=NOW,
        )


class PassingProbe:
    """真实返回正面能力/基准证据的 probe fake。"""

    def __init__(self, request: DatasetRequest, *, throughput_mib_s: float = 50.0) -> None:
        self.request = request
        self.throughput_mib_s = throughput_mib_s
        self.calls: list[str] = []

    def probe(self, route: Route, request: DatasetRequest, adapter: object) -> ProbeResult:
        self.calls.append(route.id)
        return ProbeResult(
            route_id=route.id,
            status=CheckStatus.PASS,
            dns_ok=True,
            endpoint_ok=True,
            auth_ok=True,
            metadata_ok=True,
            object_ok=True,
            request_ok=True,
            response_valid=True,
            bytes_transferred=1024,
            latency_ms=80.0,
            benchmark=benchmark(self.throughput_mib_s, latency_ms=80.0),
            capabilities=all_pass_capabilities(self.request),
            checked_at=NOW,
        )


class StaticAdapter:
    """真实现内存 adapter（不是 mock）；用于 materialize 的 source 边界测试。"""

    def __init__(self, source_id: str, assets: Sequence[SourceAsset]) -> None:
        self.source_id = source_id
        self.supported_routes = frozenset({"https"})
        self._assets = list(assets)
        self.calls: list[str] = []

    def describe(self) -> object:
        raise AssertionError("not used")

    def discover_assets(self, request: DatasetRequest) -> Sequence[SourceAsset]:
        self.calls.append(request.request_id)
        return tuple(self._assets)

    def health_check(self) -> bool:
        return True


# --------------------------------------------------------------------------- #
# 五态与发现
# --------------------------------------------------------------------------- #
def test_unknown_and_not_implemented_are_not_failures() -> None:
    unknown = RouteCapabilities()
    not_impl = RouteCapabilities(transport=CheckStatus.NOT_IMPLEMENTED)
    assert aggregate_checks(unknown) is CheckStatus.UNKNOWN
    assert aggregate_checks(not_impl) is CheckStatus.NOT_IMPLEMENTED


def test_capability_check_keeps_unknown_distinct_and_marks_explicit_dataset_mismatch() -> None:
    req = load_era5_requirement()
    unknown = route(
        "unknown-era5-https",
        source="mirror",
        transport="https",
        request=req,
        capabilities=RouteCapabilities(dataset=CheckStatus.PASS, transport=CheckStatus.PASS),
    )
    checked = check_capabilities(req, unknown)
    assert checked.variables
    assert all(status is CheckStatus.UNKNOWN for status in checked.variables.values())
    mismatch = unknown.model_copy(update={"dataset": "cmip6"})
    assert check_capabilities(req, mismatch).dataset is CheckStatus.FAIL


def test_route_json_round_trip_keeps_computed_summary_and_rejects_unknown_fields() -> None:
    req = load_era5_requirement()
    target = route("cds-era5", source="cds", transport="cds_api", request=req)
    payload = target.model_dump(mode="json")
    assert payload["capability_summary"] == "PASS"
    assert type(target).model_validate(payload).capability_summary is CheckStatus.PASS
    payload["unexpected_field"] = "must fail"
    with pytest.raises(ValueError, match="unexpected_field"):
        type(target).model_validate(payload)


def test_era5_fixture_discovers_multiple_routes_without_cds_exclusivity() -> None:
    req = load_era5_requirement()
    configs = SourcesConfig(
        sources={
            "cds": SourceConfig(
                provider="Copernicus",
                display_name="CDS",
                adapter="cds",
                implemented=True,
                datasets=[req.dataset_id],
                transports=[
                    TransportBinding(route=AccessRoute.CDS_API, adapter="cds_api", implemented=True)
                ],
                endpoint="https://cds.example.test",
            ),
            "ncar": SourceConfig(
                provider="NCAR",
                display_name="RDA",
                adapter="rda",
                implemented=True,
                datasets=[req.dataset_id],
                transports=[
                    TransportBinding(route=AccessRoute.HTTPS, adapter="https", implemented=True),
                    TransportBinding(route=AccessRoute.S3, adapter="s3", implemented=False),
                    TransportBinding(route=AccessRoute.GLOBUS, adapter="globus", implemented=False),
                ],
                endpoint="https://rda.example.test",
            ),
            "mirror": SourceConfig(
                provider="Mirror",
                display_name="Mirror",
                adapter="rda",
                implemented=False,
                datasets=[req.dataset_id],
                transports=[
                    TransportBinding(route=AccessRoute.HTTPS, adapter="https", implemented=False)
                ],
            ),
        }
    )
    engine = RouteIntelligence(sources=configs, clock=lambda: NOW)
    plan = engine.plan(req)

    assert len(plan.candidate_routes) == 5
    assert {item.source for item in plan.candidate_routes} == {"cds", "ncar", "mirror"}
    assert len({item.transport for item in plan.candidate_routes}) == 4
    unimplemented = next(item for item in plan.candidate_routes if item.source == "mirror")
    assert unimplemented.adapter_status is AdapterImplementationStatus.NOT_IMPLEMENTED
    assert unimplemented.capabilities.transport is CheckStatus.NOT_IMPLEMENTED
    assert "no data-unavailability conclusion" in " ".join(plan.notes)


# --------------------------------------------------------------------------- #
# Case A–F：偷懒行为
# --------------------------------------------------------------------------- #
def test_case_a_qualified_cds_and_unknown_ncar_does_not_claim_only_cds() -> None:
    req = load_era5_requirement()
    cds = route(
        "cds-era5",
        source="cds",
        transport="cds_api",
        request=req,
        qualification=RouteQualification.QUALIFIED,
        status=RouteStatus.AVAILABLE,
        bench=benchmark(10),
    )
    ncar = route("ncar-era5-https", source="ncar", transport="https", request=req)
    engine = RouteIntelligence(prober=RecordingProbe(), clock=lambda: NOW)
    engine.register_routes([cds, ncar])

    plan = engine.plan(req)
    evaluation = engine.evaluate(plan, auto_probe=False)

    assert evaluation.selected is not None
    assert evaluation.selected.id == cds.id
    assert ncar.id in evaluation.needs_probe
    assert "only" not in evaluation.selection_reason.lower()
    assert {item.route_id for item in evaluation.decisions} == {cds.id, ncar.id}


def test_unknown_route_enters_low_cost_probe() -> None:
    req = load_era5_requirement()
    ncar = route("ncar-era5-https", source="ncar", transport="https", request=req)
    probe = RecordingProbe(CheckStatus.UNKNOWN)
    engine = RouteIntelligence(prober=probe, clock=lambda: NOW)
    engine.register_routes([ncar])
    plan = engine.plan(req)
    engine.evaluate(plan)
    assert probe.calls == [ncar.id]


def test_probe_can_qualify_unknown_route_then_allow_large_download() -> None:
    req = load_era5_requirement()
    ncar = route("ncar-era5-https", source="ncar", transport="https", request=req)
    probe = PassingProbe(req, throughput_mib_s=80)
    engine = RouteIntelligence(prober=probe, clock=lambda: NOW)
    engine.register_routes([ncar])
    evaluation = engine.evaluate(engine.plan(req), auto_probe=True)
    assert probe.calls == [ncar.id]
    assert evaluation.selected is not None
    assert evaluation.selected.qualification is RouteQualification.QUALIFIED
    assert evaluation.selected.benchmark is not None
    assert engine.authorize_download(engine.plan(req)).allowed is True


def test_probe_without_benchmark_does_not_qualify() -> None:
    req = load_era5_requirement()
    ncar = route("ncar-era5-https", source="ncar", transport="https", request=req)
    engine = RouteIntelligence(prober=RecordingProbe(CheckStatus.PASS), clock=lambda: NOW)
    engine.register_routes([ncar])
    evaluation = engine.evaluate(engine.plan(req), auto_probe=True)
    assert evaluation.selected is None
    result, updated = engine.qualify(engine.plan(req), ncar.id)
    assert result.status is CheckStatus.PASS
    assert updated.qualification is RouteQualification.DISCOVERED


def test_case_b_faster_qualified_ncar_wins_without_cds_special_case() -> None:
    req = load_era5_requirement()
    cds = route(
        "cds-era5",
        source="cds",
        transport="cds_api",
        request=req,
        qualification=RouteQualification.QUALIFIED,
        status=RouteStatus.AVAILABLE,
        bench=benchmark(10),
    )
    ncar = route(
        "ncar-era5-https",
        source="ncar",
        transport="https",
        request=req,
        qualification=RouteQualification.QUALIFIED,
        status=RouteStatus.AVAILABLE,
        bench=benchmark(100),
    )
    engine = RouteIntelligence(clock=lambda: NOW)
    engine.register_routes([cds, ncar])
    evaluation = engine.evaluate(engine.plan(req), objective="min_time")
    assert evaluation.selected is not None
    assert evaluation.selected.id == ncar.id
    assert "throughput=100.0MiB/s" in evaluation.decisions[0].rationale


def test_case_c_only_qualified_cds_is_allowed() -> None:
    req = load_era5_requirement()
    cds = route(
        "cds-era5",
        source="cds",
        transport="cds_api",
        request=req,
        qualification=RouteQualification.QUALIFIED,
        status=RouteStatus.AVAILABLE,
        bench=benchmark(10),
    )
    engine = RouteIntelligence(clock=lambda: NOW)
    engine.register_routes([cds])
    selection = engine.select(engine.plan(req))
    assert selection.route.id == cds.id


def test_case_d_unimplemented_adapter_is_not_data_unavailable() -> None:
    req = load_era5_requirement()
    ncar = route(
        "ncar-era5-https",
        source="ncar",
        transport="https",
        request=req,
        adapter_status=AdapterImplementationStatus.NOT_IMPLEMENTED,
        capabilities=RouteCapabilities(
            dataset=CheckStatus.PASS,
            transport=CheckStatus.NOT_IMPLEMENTED,
        ),
        status=RouteStatus.UNKNOWN,
    )
    engine = RouteIntelligence(clock=lambda: NOW)
    engine.register_routes([ncar])
    plan = engine.plan(req)
    evaluation = engine.evaluate(plan, auto_probe=False)
    assert evaluation.selected is None
    assert evaluation.needs_probe == []
    assert any("adapter is not implemented" in note for note in plan.notes)
    assert "does not provide" not in " ".join(plan.notes)


def test_case_e_unknown_large_download_is_blocked() -> None:
    req = load_era5_requirement()
    unknown = route("ncar-era5-https", source="ncar", transport="https", request=req)
    engine = RouteIntelligence(prober=RecordingProbe(), clock=lambda: NOW)
    engine.register_routes([unknown])
    plan = engine.plan(req)
    decision = engine.authorize_download(plan)
    assert decision.allowed is False
    assert decision.requires_probe is True
    with pytest.raises(NoQualifiedRouteError):
        engine.select(plan)


def test_case_f_expired_route_requires_probe() -> None:
    req = load_era5_requirement()
    old = route(
        "ncar-era5-https",
        source="ncar",
        transport="https",
        request=req,
        qualification=RouteQualification.QUALIFIED,
        status=RouteStatus.AVAILABLE,
        bench=benchmark(30),
        last_verified=NOW - timedelta(days=30),
    )
    engine = RouteIntelligence(
        prober=RecordingProbe(CheckStatus.UNKNOWN),
        clock=lambda: NOW,
        qualification_ttl=timedelta(days=7),
    )
    engine.register_routes([old])
    evaluation = engine.evaluate(engine.plan(req), auto_probe=True)
    assert old.id in evaluation.needs_probe
    assert evaluation.selected is None


def test_large_qualified_route_still_needs_benchmark() -> None:
    req = load_era5_requirement()
    no_bench = route(
        "cds-era5",
        source="cds",
        transport="cds_api",
        request=req,
        qualification=RouteQualification.QUALIFIED,
        status=RouteStatus.AVAILABLE,
    )
    engine = RouteIntelligence(clock=lambda: NOW)
    engine.register_routes([no_bench])
    evaluation = engine.evaluate(engine.plan(req), auto_probe=False)
    assert evaluation.workload is WorkloadTier.LARGE
    assert evaluation.selected is None
    assert any(
        "requires a completed benchmark" in (item.rejection or "") for item in evaluation.decisions
    )


# --------------------------------------------------------------------------- #
# Registry 与 materialize
# --------------------------------------------------------------------------- #
def test_registry_persists_and_expires_route(tmp_path) -> None:
    req = load_era5_requirement()
    path = tmp_path / "routes.json"
    registry = RouteExperienceRegistry(path, clock=lambda: NOW)
    route_obj = route("cds-era5", source="cds", transport="cds_api", request=req)
    registry.upsert(route_obj)
    loaded = RouteExperienceRegistry(path, clock=lambda: NOW)
    assert loaded.get(route_obj.id) is not None
    registry.expire(route_obj.id)
    assert (
        RouteExperienceRegistry(path).get(route_obj.id).route.qualification
        is RouteQualification.EXPIRED
    )


def test_materialize_selects_route_source_adapter() -> None:
    req = load_era5_requirement()
    source_meta = make_source_metadata(
        source_id="ncar",
        access_route=AccessRoute.HTTPS,
        origin_uri="https://rda.example.test/file.nc",
    )
    asset = make_asset(
        source_id="ncar",
        access_route=AccessRoute.HTTPS,
        origin_uri=source_meta.origin_uri,
        metadata=source_meta,
        file_name="era5-route-fixture.nc",
    )
    adapter = StaticAdapter("ncar", [asset])
    ncar = route(
        "ncar-era5-https",
        source="ncar",
        transport="https",
        request=req,
        qualification=RouteQualification.QUALIFIED,
        status=RouteStatus.AVAILABLE,
        bench=benchmark(100),
    )
    engine = RouteIntelligence(adapters=[adapter], clock=lambda: NOW)
    engine.register_routes([ncar])
    plan = engine.materialize(
        engine.plan(req),
        planner=DefaultTaskPlanner(PlannerConfig(max_tasks_per_plan=10)),
    )
    assert all(task.asset.source_id == "ncar" for task in plan.tasks)
    assert plan.extensions["routing.selected_route_id"] == ncar.id
    assert plan.extensions["routing.requires_qualification"] is True
    assert adapter.calls == [req.request_id]


def test_download_without_executor_refuses_platform_bypass() -> None:
    req = load_era5_requirement()
    cds = route(
        "cds-era5",
        source="cds",
        transport="cds_api",
        request=req,
        qualification=RouteQualification.QUALIFIED,
        status=RouteStatus.AVAILABLE,
        bench=benchmark(10),
    )
    engine = RouteIntelligence(clock=lambda: NOW)
    engine.register_routes([cds])
    source_meta = make_source_metadata(
        source_id="cds",
        access_route=AccessRoute.CDS_API,
        origin_uri="https://cds.example.test/result.nc",
    )
    asset = make_asset(
        source_id="cds",
        access_route=AccessRoute.CDS_API,
        origin_uri=source_meta.origin_uri,
        metadata=source_meta,
        file_name="era5-route-fixture.nc",
    )
    task = make_task(asset=asset, plan_id="plan-route-fixture")
    acquisition_plan = AcquisitionPlan(
        plan_id="plan-route-fixture",
        request_id=req.request_id,
        tasks=[task],
        generated_at=NOW,
    )
    with pytest.raises(RouteExecutionError, match="no download executor"):
        engine.download(engine.plan(req), acquisition_plan=acquisition_plan)
