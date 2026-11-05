# ADR-0015：交付路径 —— 数据必须落到用户要求的位置，且不得静默覆盖

- 状态：Accepted
- 日期：2026-11-05
- 决策人：Lead（首席架构师）

## 背景

指令 §1 的典型请求是：

> 帮我获取 …ERA5 小时数据，…**保存到当前项目的 data/raw 目录**。

审计发现一个**真实存在的产品缺口**：现有实现把校验通过的文件提交到存储根
（默认 `~/.rdh/raw/<dataset_id>/`），**用户的 `data/raw` 从未被使用**。
`FileRecord.local_path` 指向的是 `~/.rdh` 内部路径。

也就是说：按现有链路跑完，"数据在 `~/.rdh/raw/…`，不在用户说的 `data/raw`"。
对 Agent 场景这是致命的——Agent 交付给用户的路径与用户预期不符。

同时指令 §13 规定"默认禁止静默覆盖已有数据"、"如果目标文件已存在，应检查是否一致，
并由用户决定覆盖、跳过或另存"。

## 决策

1. **两区架构不变**：`~/.rdh/raw/` 仍是**经过校验的权威副本**（Manifest 的事实来源）。不推翻它。
2. 新增 `researchdatahub/delivery/` 包（新模块，不动既有 storage）：

   - `resolve_target_dir(output_path, *, cwd) -> Path`：把 `output.path` 解析为绝对路径。
     **相对路径以调用方 cwd 为基准**（Agent 场景即"当前项目"）。若路径含 `..` 逃逸尝试则拒绝。
   - `plan_delivery(records, target_dir) -> DeliveryPlan`：逐文件判定
     `CREATE` / `SKIP_IDENTICAL` / `CONFLICT`：
     * 目标不存在 → `CREATE`
     * 目标存在且大小 + sha256 **完全一致** → `SKIP_IDENTICAL`（幂等，不覆盖、不重写）
     * 目标存在但内容不同 → `CONFLICT`（**不覆盖**）
   - `apply_delivery(plan, *, on_conflict) -> DeliveryResult`，`on_conflict ∈ {raise, skip, overwrite, suffix}`：
     * 默认 `raise`：返回结构化冲突列表，交由调用方（Agent → 用户）决定；
     * `skip` 跳过冲突文件；`overwrite` 仅在**用户显式选择**时允许；
     * `suffix` 另存为 `name.ext.1` / `name.ext.2`（绝不动原文件）。
   - 交付用 **copy + fsync + 原子 rename**（先写 `<name>.rdh-tmp-<rand>`，再同目录 `os.replace`），
     不跨卷 rename，不做软链接（软链接会让用户目录在 `~/.rdh` 被清空时静默失效）。
   - 权限：交付后的文件**不是**只读（它是用户的文件，用户可改），与 `~/.rdh/raw` 的只读语义区分开。

3. **Manifest 记录双路径**：`FileRecord.extensions` 增加
   `rdh.delivery_path`（用户位置）与 `rdh.delivery_status`（`delivered` / `skipped_identical` / `conflict`）。
   `local_path` **保持**指向权威副本，不改变既有语义（避免破坏 979 个既有测试与既有代码）。
4. **空间不足优先失败**：交付前检查目标盘剩余空间，不足则**在任何写操作之前**报错。
5. MCP 的 `get_manifest` 返回**交付路径**（用户位置）+ 权威副本路径 + 冲突列表。

## 后果

**正面**

- 用户与 Agent 拿到的是**用户要的路径**，这是指令 §18 最终验收的必要条件；
- 权威副本与用户副本分离：用户清空 `data/raw` 不影响可复现性（Manifest 仍指向 `~/.rdh/raw`）；
- 冲突语义明确，默认不覆盖，符合 §13。

**负面 / 维护成本**

- 磁盘占用约为数据的 2 倍（权威副本 + 用户副本）。对于大体积数据（TB 级）不可忽视。
  缓解：`apply_delivery` 支持 `move=True`（默认 `False`）—— 用户明确知道自己在做什么时
  可以"移动而非复制"，把权威副本的路径改为交付路径并更新 Manifest。**默认不启用**，文档说明。
- 需要额外的交付阶段（任务完成后）。放在 `acquisition` 需求级流程的 `VERIFYING → COMPLETED` 之间，
  失败时需求级进入 `PARTIALLY_SUPPORTED` 或 `FAILED`（取决于是否已部分交付）。

## 被否决的方案

1. **直接把存储根改成用户指定目录**：会让 `~/.rdh` 的原子提交/隔离区/只读保护机制失效，
   且用户目录被删除时 Manifest 全部悬空。**否决**。
2. **用符号链接指向 `~/.rdh/raw`**：用户目录看起来"有文件"，但实测在备份、打包、Docker
   挂载等场景下软链接会断裂或被忽略，Agent 读取时拿到空目录。**否决**。
3. **默认覆盖已有文件**：违反指令 §13。**否决**。
4. **交给 Agent 自己做复制**：把"原子性、fsync、冲突检测、空间检查"这些容易做错的逻辑推给 LLM
   生成的脚本，正是本项目要消除的事。**否决**。
