# 深度登记 扫描期版质检台

- 规模：中型
- 核心规则：期号、版次、页码构成业务键，图像相似只生成候选，页眉证据决定混期。
- 使用者与工作流：质检员导入扫描清单，核对期版与页序，裁决重扫和混期，归档证据。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 清单采信 | 导入扫描清单并定位坏行 | POST /api/scans/import | `app/modules/m1_intake/parser.py` `app/modules/m1_intake/integrity.py` | `tests/test_m1_intake.py` |
| M2 期版契约 | 规范期号、版次和页码 | POST /api/issues/normalize | `app/modules/m2_issue/rules.py` `app/modules/m2_issue/catalog.py` | `tests/test_m2_issue.py` |
| M3 页码序列 | 查缺页、重页与越界页 | POST /api/pages/check | `app/modules/m3_pages/sequence.py` `app/modules/m3_pages/reconcile.py` | `tests/test_m3_pages.py` |
| M4 重扫判定 | 比较扫描候选并保留清晰版本 | POST /api/rescans/evaluate | `app/modules/m4_rescans/similarity.py` `app/modules/m4_rescans/decision.py` | `tests/test_m4_rescans.py` |
| M5 混期归因 | 用页眉和页码证据裁决混期 | POST /api/mixed/evaluate | `app/modules/m5_mixed/evidence.py` `app/modules/m5_mixed/conflicts.py` | `tests/test_m5_mixed.py` |
| M6 批次对账 | 检查四账并导出证据 | POST /api/reconcile | `app/modules/m6_batches/compare.py` `app/modules/m6_batches/exporter.py` | `tests/test_m6_batches.py` |
| M7 验收签章 | 计划版本发布、签章冻结、修订复验与回读 | POST /api/acceptance/plans `/seals` `/rechecks` | `app/modules/m7_acceptance/service.py` `app/modules/m7_acceptance/repository.py` | `tests/test_m7_acceptance.py` `tests/test_m7_domain.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 首列带 BOM | 期号字段取不到 | 读取表头时移除 BOM | `app/modules/m1_intake/parser.py` | `tests/test_m1_intake.py::test_strip_bom_header` |
| TRAP-05 | 行列数不齐 | 错位行进入页序 | 隔离并保留物理行号 | `app/modules/m1_intake/parser.py` | `tests/test_m1_intake.py::test_column_count_goes_quarantine` |
| TRAP-07 | 页眉为空 | 空串被当作一致 | 返回未知并转人工复核 | `app/modules/m5_mixed/evidence.py` | `tests/test_m5_mixed.py::test_missing_header_is_unknown` |
| TRAP-08 | 前导零页码 | 转整数后无法复核原编号 | 规范为三位字符串 | `app/modules/m2_issue/rules.py` | `tests/test_m2_issue.py::test_page_zero_pads_without_numeric_roundtrip` |
| TRAP-09 | 大摘要或相似哈希 | 数字往返或只凭相似度误判 | 摘要保形，相似仅生成候选 | `app/modules/m5_mixed/conflicts.py` | `tests/test_m5_mixed.py::test_hash_candidate_without_header_needs_review` |
| TRAP-14 | 同版多扫描值冲突 | 后写覆盖清晰版本 | 版本、清晰度、时间稳定择优 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_duplicate_page_detected` |
| TRAP-16 | 清单尾行截断 | 半行被当成扫描件 | 完整尾行检查并隔离 | `app/modules/m1_intake/integrity.py` | `tests/test_m1_intake.py::test_truncated_tail_detected` |
| TRAP-19 | 候选顺序改变 | 先到者决定保留项 | 显式排序键保持顺序无关 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_selection_order_independent` |
| TRAP-22 | 同内容多别名 | 合并顺序改变代表扫描 | 清晰度后以时间和文件名择一 | `app/modules/m4_rescans/decision.py` | `tests/test_m4_rescans.py::test_selection_is_deterministic_on_tie` |
| TRAP-26 | 陈旧重扫晚提交 | 旧版本覆盖新版 | 版本优先于输入顺序 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_higher_version_wins_not_later_input` |
| TRAP-30 | 更正稿直接改页数 | 已签章凭据被篡改 | 计划只追加新版本；签章冻结 plan_digest/scope_digest，原件不可变 | `app/modules/m7_acceptance/repository.py` | `tests/test_m7_acceptance.py::test_seal_freezes_plan_and_scope_and_keeps_conclusion_on_later_revision` |
| TRAP-31 | 沿用旧稿验收新批次 | 误报漏页/越界 | 签章显式指定 plan_version，按指定版核对四账 | `app/modules/m7_acceptance/service.py` | `tests/test_m7_acceptance.py::test_recheck_detects_newly_missing_and_out_of_range_under_revision` |
| TRAP-32 | 复验覆盖原件 | 历史结论丢失 | 复验写 m7_rechecks 并产出 diff，签章表无更新路径 | `app/modules/m7_acceptance/repository.py` | `tests/test_m7_acceptance.py::test_recheck_against_revised_plan_saves_diff_without_overwriting_seal` |
| TRAP-33 | 计划发布与签章并发 | 陈旧客户端覆盖新版本 | expected_version 乐观校验 + (issue_edition,version) 唯一约束，冲突返 409 | `app/modules/m7_acceptance/service.py` | `tests/test_m7_acceptance.py::test_stale_publish_with_old_expected_version_is_rejected` |
| TRAP-34 | 复验中断后重跑 | 重复生成复验结果 | 确定性 job_id/recheck_id 幂等，续跑只处理未完期版 | `app/modules/m7_acceptance/service.py` | `tests/test_m7_acceptance.py::test_recheck_job_resumes_after_interruption_without_duplicate_results` |
| TRAP-35 | 扫描范围在签章后漂移 | 复验凭据与原件不符 | 复验重算 scope_digest 与冻结值比对，漂移返 `SCOPE_DRIFT` | `app/modules/m7_acceptance/service.py` | `tests/test_m7_acceptance.py` |

## 对账规则

- 应有页数 = 唯一采纳页 + 缺页数。
- 扫描件数 = 唯一采纳 + 被替代重扫 + 混期 + 隔离。
- 同一输入与规则版本重跑，运行标识和结果行数不变。

## 已知不覆盖

- 不读取图像二进制，感知哈希与 OCR 页眉由上游扫描软件提供。
- M7 不动 M4 重扫裁决，不做补扫派工；只在采纳清单上核对应有/实有/缺失/越界页。
- M7 复验沿用签章冻结的扫描范围，不重新采信清单、不修改任何签章原件。

