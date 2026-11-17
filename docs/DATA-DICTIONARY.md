# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。

## M7 验收签章与修订复验

- issue_edition：期版业务键，规范化为 `YYYY-MM-DD|EDITION`（增刊保留 `S1` 形态），字符串存储。
- plan_version：同一期版页码计划的单调递增版本；发布只追加，不改写历史版本。
- basis：计划依据（勘误函号/来函/审批说明），为空拒绝发布，错误码 `BASIS_REQUIRED`。
- plan_digest：期版键+版本+应有页+依据的规范 JSON SHA-256，签章时冻结。
- scope_digest：签章时刻采纳扫描行（页、文件、sha256、版本、清晰度）的规范 JSON SHA-256；复验时重算必须与冻结值一致，否则 `SCOPE_DRIFT`。
- seal_id：`SEAL-` + 期版键、计划版本、plan_digest、scope_digest 组合摘要前 12 位；同计划同范围重试复用，不重复签章。
- recheck_id：`CHK-` + 签章标识与目标计划版本组合摘要前 12 位；复验中断重试不重复生成。
- 核对四账：expected_pages（应有，整数页号）、actual_pages（实有，重页折叠）、missing_pages（应有实无）、out_of_range_pages（实有但不在应有集合）；duplicate_pages 单独报告。
- 签章结论：无缺页且无越界为 `PASS`，否则 `FAIL`；签章后原件不可变，复验结论与差异另存于 m7_rechecks。
- 并发：计划发布与签章携带期望版本号；陈旧提交返回 409（`PLAN_VERSION_CONFLICT` / `STALE_SEAL_REQUEST`），数据库唯一约束兜底并发竞争。
- 复验任务：job_id 由目标版本与全部待验签章确定性派生；状态 `RUNNING/INTERRUPTED/DONE`，恢复时只处理未完期版。

