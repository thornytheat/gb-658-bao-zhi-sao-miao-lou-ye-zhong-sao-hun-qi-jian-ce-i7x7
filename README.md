# 扫描期版质检台

面向真实数据复核的中型数据工程工具，包含六个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18217。测试：docker compose exec -T app pytest -q

## 模块

- M1 清单采信：页面 /intake，接口 /api/scans/import
- M2 期版契约：页面 /issues，接口 /api/issues/normalize
- M3 页码序列：页面 /pages，接口 /api/pages/check
- M4 重扫判定：页面 /rescans，接口 /api/rescans/evaluate
- M5 混期归因：页面 /mixed，接口 /api/mixed/evaluate
- M6 批次对账：页面 /batches，接口 /api/reconcile
- M7 验收签章：页面 /acceptance，接口前缀 /api/acceptance（计划发布、验收签章、复验、回读）

## M7 接口速览

- `POST /api/acceptance/scans/import`：采信真实扫描清单（坏行隔离，同内容幂等复用）
- `POST /api/acceptance/plans`：按期版发布带依据的页码计划，`expected_version` 做乐观并发控制
- `POST /api/acceptance/seals`：按计划版本签章，冻结 plan_digest 与 scope_digest
- `POST /api/acceptance/rechecks`：历史签章按新稿复验，差异另存、原件不动
- `POST /api/acceptance/recheck-jobs`：批量复验，中断后再次调用恢复未完期版
- `GET /api/acceptance/issue-edition?issue=&edition=`：回读各版应有、实有、缺失、越界页

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

