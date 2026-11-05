# ADR-0011：有副作用操作的 HMAC 审批令牌模型

- 状态：Accepted
- 日期：2026-11-05
- 决策人：Lead（首席架构师）

## 背景

指令 §8 要求："有副作用的工具必须具备权限与状态检查。`submit_acquisition` 必须要求有效的 execution plan ID，
并验证计划是否已经得到必要授权。不能因为 Agent 说了'下载'就绕过参数验证和授权流程。"
§7 Step 7 补充："对消耗费用、配额或可能产生外部副作用的任务，默认要求用户确认。"

这带来一个真实的授权难题：**用户是在 Agent 的对话里"确认"的，而 ResearchDataHub Core 看不到对话**。
如果 Core 无法区分"用户真的确认过"与"Agent 自己决定要下载"，那么授权形同虚设。

## 决策

采用 **HMAC 签名的审批令牌（approval token）**：

1. `create_execution_plan` 生成计划后，计算**计划指纹**
   `fingerprint = sha256(canonical_json(plan_id, request_fingerprint, tasks[asset_id, origin_uri, size, route], 版本))`。
2. 生成令牌：

   ```
   token = base64url(payload) + "." + base64url(HMAC-SHA256(key, payload_b64))
   payload = {
     "v": 1,
     "plan_id": "...",
     "plan_fingerprint": "...",
     "issued_at": "<UTC ISO8601>",
     "expires_at": "<UTC ISO8601，默认 +30 min>",
     "jti": "<随机 nonce>",
     "max_bytes": <计划声明总量>,
   }
   ```

3. 密钥存放 `~/.rdh/approval.key`：**首次使用时自动生成** 32 字节随机密钥，
   权限 `0600`（Windows 上设置 ACL 不可行时至少不在库内、不在仓库内），**绝不写入 manifest、日志、token 本身**。
4. `submit_acquisition(plan_id, approval_token)` 必须全部满足才执行：

   | 检查 | 失败返回 |
   |---|---|
   | 令牌签名有效（常量时间比较 `hmac.compare_digest`） | `approval_invalid` |
   | `payload.plan_id == 请求的 plan_id` | `approval_invalid` |
   | `payload.plan_fingerprint` 与**当前**计划的指纹一致（防止计划在批准后被篡改） | `approval_plan_changed` |
   | 未过期 | `approval_expired` |
   | `jti` 未被使用过（一次性，防重放） | `approval_replayed` |
   | 计划存在且处于 `AWAITING_APPROVAL`（或 `PLANNED` 且未提交过） | `invalid_state` |

5. `jti` 用后即焚：写入 `mcp_approvals` 表（唯一约束），重复即拒。
6. **防重复提交**：同一 `plan_id` 已有任务组时，返回既有任务组并标 `already_submitted: true`，**不重复入队**。
7. 审批记录（`plan_id`、指纹、`issued_at`、`used_at`、`jti`）持久化，供事后审计；**不存令牌原文**（存其 sha256）。

## 后果

**正面**

- Agent 无法自行发起下载：令牌只能由 `create_execution_plan` 签发，而该工具**不执行**任何获取；
- 可以证明"用户看到的那份计划"就是"被执行的那份计划"（指纹绑定），防止批准后被篡改；
- 一次性 + 过期 + 重放检测，杜绝"一次批准多次执行"；
- 无额外服务、无密钥分发问题（本机单用户场景下自签自验合理）。

**负面 / 真实限制（必须写进文档，不能假装没有）**

- **Agent 可以在同一轮里先 `create_execution_plan` 再 `submit_acquisition`**，即令牌机制只能保证
  "计划未被篡改且只执行一次"，**不能**在协议层强制"用户确实点了确认"。
  真正的用户确认由 **Skill（`data-acquisition`）** 在提示词层强制（要求 Agent 先展示计划并取得明确同意），
  这是 LLM Agent 场景下的已知局限。README 必须如实说明，不得宣称"技术手段保证用户确认"。
- 单机多用户共享 `~/.rdh/approval.key` 时权限隔离不足；V1 的单用户假设需写明。

## 被否决的方案

1. **不给令牌，只校验 plan 存在**：Agent 可自行创建计划并提交，授权名存实亡。**否决**。
2. **在 Core 里检查"用户在对话里确认"**：Core 看不到对话，且这会要求 Core 依赖 LLM/Agent 上下文（违反 §17.7）。**否决**。
3. **用非对称签名（Ed25519）**：单机自签自验下，非对称只增加依赖与复杂度，无安全增益。**否决**。
4. **把令牌存进数据库由服务端签发/校验**：等价于"随机字符串 + DB 查表"，无法离线验证计划指纹绑定。**否决**。
