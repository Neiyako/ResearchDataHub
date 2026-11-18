# ADR-0016：在 Adapter 与 Planner 之间增加 Route Intelligence

- 状态：Accepted
- 日期：2026-11-18

## 背景

RDH 已有 source adapter、transport adapter、planner、manifest 和实测 worker route，
但数据集级决策仍由 `preferred_sources` 与“首个满足变量的适配器”隐式完成。Agent 可能因此直接
选择自己最熟悉的 CDS，即使 NCAR HTTPS 或其他 transport 尚未被验证。

## 决策

新增 `researchdatahub.routing` 作为**数据集 route 规划层**，复用既有 source/transport/planner：

1. `RoutePlan` 从 source config 与 registry 发现候选 route，保留未实现和不匹配项；
2. `RouteCapabilities` 用 PASS/FAIL/UNKNOWN/NOT_IMPLEMENTED/NOT_TESTED 五态；
3. `DefaultConnectivityProbe` 只做低字节量 health check；source-specific benchmark 通过注入；
4. `RouteExperienceRegistry` 使用同目录原子 JSON，不使用新数据库；
5. `RouteIntelligence.select()` 只允许 qualified route；large workload 必须有 benchmark；
6. `RouteIntelligence.download()` 先过 policy，再由现有 scheduler/transport 执行器搬字节。

不重写 CDS adapter、CLI、MCP 或 manifest 状态机。

## 后果

- 默认 discovery 会显示多条 route，并将未实现 transport 明确标成 `NOT_IMPLEMENTED`；
- 在真实 source-specific probe 接入前，health check 可能只产生 `UNKNOWN/NOT_TESTED`，不会
  误发 qualification；这是有意的保守行为；
- 经验注册表是 route 级旁路状态，不能替代 manifest 的任务状态；
- `RouteManager` 的 worker 选路与 dataset route 选路保持分层。

## 被否决的方案

- 直接把 `cds` 设为默认并继续增加 Adapter：只扩大可调用面，不能证明其他 route 可用，也会把平台熟悉度固化为策略。
- 把所有 route 状态塞进 `manifest.db`：会混淆任务事实与 route 经验，且让只读规划也依赖任务库。
- 用 ML 预测 route 质量：第一版缺乏足够样本，且不可解释；当前只用真实 probe/benchmark 与保守规则。
