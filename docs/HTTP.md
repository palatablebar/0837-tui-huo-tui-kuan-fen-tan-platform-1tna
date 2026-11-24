# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿；请求可给 `profile`、自定义 `rows`/`records`、`csv` 文本、`path`，或 multipart `file`。
- `GET /api/runs/{id}`：读取批次（含 `review_board`、`orders`、`order_reviews`、`fulfillment`）。
- `GET /api/runs/{id}/review`：只读取批次复核状态与订单复核板。
- `POST /api/runs/{id}/review`：整批签发或暂缓，提交 `decision`、`comment`、`expected_revision`；带 `order_id` 时退化为订单级签发/暂缓/补正/重试。
- `POST /api/runs/{id}/orders/{order}/issue|hold|release|correct|retry`：订单级流程。
- `POST /api/runs/{id}/orders/{order}/rectify`：`correct` 的别名。
- `POST /api/runs/{id}/issue`、`/hold`、`/correct`、`/corrections`、`/rectify`、`/retry`：订单标识放在请求体里的扁平别名；`/corrections` 还接受 `{line_id: values}` 映射或操作数组。
- `POST /api/runs/{id}/issue-all`：签发所有 `READY` 订单，暂缓/阻断订单跳过。
- `PUT /api/runs/{id}`（及 `POST /api/runs/{id}/replace`）：用补正后的完整 `rows`/`csv` 整体替换来源并重算。
- `GET /api/runs/{id}/vouchers[/{order}]`：已签发订单的冻结凭证。
- `GET /api/runs/{id}/evidence`：整批签发后下载证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

## 订单复核板

`review_board` 中：

- `batch_hold=true`：存在认不出归属的坏行，所有订单都不能签发，签发返回 `409 BATCH_HELD`。
- `orders[].status`：`READY`（可签发）、`HELD`（坏行/人工暂缓）、`BLOCKED`（计算阻断，如 `OVER_REFUND`）、`ISSUED`（已签发，金额与凭证冻结）。
- `orders[].amount_version` 与 `pending_amount`：当前有效待签金额版本；补正导致金额变化时版本 +1，并把旧值放在 `invalidated_amount`，用旧版本签发返回 `409 STALE_AMOUNT`。
- 订单签发天然幂等；也支持 `Idempotency-Key` 请求头或 `idempotency_key` 字段。重试返回同一张凭证，不会重复签发。

## 错误码

除既有 `RUN_NOT_FOUND`、`STALE_REVIEW`、`GATE_BLOCKED`、`NOT_APPROVED`、`BAD_REQUEST` 外，新增
`BATCH_HELD`、`ORDER_HELD`、`ORDER_BLOCKED`、`ORDER_NOT_FOUND`、`ORDER_ALREADY_ISSUED`、
`STALE_AMOUNT`、`CORRECTION_FAILED`、`CORRECTION_TARGET_NOT_FOUND`、`AMOUNT_UNAVAILABLE`、
`IDEMPOTENCY_CONFLICT`。
