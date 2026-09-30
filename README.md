# Payment Ledger Service（本地支付账本服务）

一个只依赖本地 SQLite、**不调用任何真实支付渠道 / 外部服务 / 凭据** 的 FastAPI 服务。
用一个可注入故障的“模拟支付渠道”驱动一笔支付从授权到结算/撤销的完整链路，
并以复式记账保证账目在重试、并发、异常下始终一致。

## 快速开始

```bash
uv sync
uv run pytest -q -s          # 跑全部自动化测试（带输入/判定日志）
uv run python main.py        # 启动 HTTP 服务 http://127.0.0.1:8000
```

默认数据库文件为 `data/payment_ledger.db`（可用环境变量 `PAYMENT_LEDGER_DB` 覆盖），
首次访问自动建表。交互式文档：`http://127.0.0.1:8000/docs`。

### 一条完整链路的 curl 示例

```bash
curl -s localhost:8000/payments/authorize -H 'Content-Type: application/json' -d '{
  "payment_id":"P-1","amount":1000,"currency":"USD","idempotency_key":"auth-1"}'

curl -s localhost:8000/payments/capture -H 'Content-Type: application/json' -d '{
  "payment_id":"P-1","amount":600,"currency":"USD","idempotency_key":"cap-1"}'

curl -s localhost:8000/payments/refund -H 'Content-Type: application/json' -d '{
  "payment_id":"P-1","amount":200,"currency":"USD","idempotency_key":"ref-1"}'

curl -s localhost:8000/payments/void -H 'Content-Type: application/json' -d '{
  "payment_id":"P-1","idempotency_key":"void-1"}'   # 撤销剩余 200 未捕获授权

curl -s localhost:8000/payments/P-1
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/payments/authorize` | 授权，建立支付与额度 |
| POST | `/payments/capture` | 捕获（支持多次部分捕获） |
| POST | `/payments/refund` | 退款（支持多次部分退款） |
| POST | `/payments/void` | 撤销**尚未捕获的剩余授权额度** |
| GET  | `/payments/{payment_id}` | 查询状态与金额汇总 |
| GET  | `/health`、`/` | 健康检查、骨架原端点 |

所有写接口都要求 `idempotency_key`。响应包含金额汇总以及本次动作产生的复式分录，
并带 `replayed` 字段标识该响应是否为幂等重放。

## 状态流转规则

状态由金额**派生**（不是可被随意写入的字段）：

| 状态 | 含义 |
| --- | --- |
| `AUTHORIZED` | 已授权，未捕获、未撤销 |
| `PARTIALLY_CAPTURED` | 捕获了一部分，仍有开放授权 |
| `PARTIALLY_CAPTURED_REFUNDED` | 部分捕获且已发生部分退款，仍有开放授权 |
| `CAPTURED` | 授权额度已全部处置（全捕获，或部分捕获后撤销了剩余额度） |
| `PARTIALLY_REFUNDED` | 额度已关闭，已捕获金额中退了一部分 |
| `REFUNDED` | 已捕获金额被全额退回（可退余额为 0） |
| `VOIDED` | 未发生任何捕获，授权被全额撤销 |

关键规则：

- 金额恒等式（服务每次写事务提交前自检，DB 层还有 CHECK 约束兜底）：
  - `amount_captured + amount_voided <= amount_authorized`
  - `amount_refunded <= amount_captured`
  - `refundable_amount = amount_captured - amount_refunded`
  - `open_authorized_amount = amount_authorized - amount_captured - amount_voided`
- 当 `captured + voided == authorized` 时授权额度自动关闭，之后再捕获/撤销一律拒绝。
- 非法流转不会静默改状态，每类原因对应**独立错误码**：

| 错误码 | 触发场景 | HTTP |
| --- | --- | --- |
| `payment_not_found` | 操作不存在的支付 | 404 |
| `payment_already_exists` | 对已存在 payment_id 再次授权 | 409 |
| `invalid_amount` | 金额不是正整数 | 422 |
| `currency_mismatch` | 请求币种与支付币种不一致 | 422 |
| `capture_not_allowed` | 已撤销/额度关闭后再捕获 | 409 |
| `capture_exceeds_authorized` | 捕获额超过剩余开放授权 | 409 |
| `refund_exceeds_captured` | 尚无任何捕获就退款 | 409 |
| `refund_exceeds_refundable` | 退款超过 `captured - refunded` | 409 |
| `void_not_allowed` | 已无开放授权可撤销（含撤销后再撤销） | 409 |
| `idempotency_conflict` | 幂等键被不同请求内容复用 / 记录处于中间态 | 409 |
| `gateway_timeout/declined/error` | 模拟渠道故障，事务回滚不留痕 | 502 |

## 金额与币种约定

- 金额一律是 **最小货币单位（minor unit）的正整数**（如 `1234` 表示 12.34 USD）；
  全链路不使用浮点数，不做小数换算，避免精度问题。
- 币种为三位 ISO 4217 代码（大小写不敏感，存储统一转大写）。
- 一笔支付只允许一种币种：捕获/退款币种必须与授权一致，否则 `currency_mismatch`；
  不同币种绝不混算。

## 幂等规则

- 每个写请求带 `idempotency_key`（在服务端全局唯一，而不是“每个支付一个”）。
- 服务端保存 `(action, payment_id, amount, currency)` 的请求指纹：
  - **同 key + 同内容**：首次请求执行一次；之后任何重复（含并发）都返回首次结果，
    `replayed=true`，不再次记账、不再次扣减可退余额。
  - **同 key + 不同内容**（金额、币种、支付或动作任一不同）：返回
    `409 idempotency_conflict`，不会“顺手”执行新请求。
- 实现方式：每个动作在单个 `BEGIN IMMEDIATE` 事务里完成
  “登记幂等键 → 校验 → 模拟渠道调用 → 更新汇总 → 写分录 → 落地结果 → 提交”。
  并发事务在 SQLite 层串行化，且幂等表有 UNIQUE 约束兜底，因此并发重复提交
  最多只有一个事务执行，其余全部重放。
- 任何步骤抛错（包括模拟渠道超时/拒绝）事务整体回滚，不残留
  `started` 幂等记录、不残留分录、金额不变；客户端可用**同一个 key** 安全重试。

## 复式记账

每次成功的金额变动产生**借贷平衡**的分录（`ledger_entries` 表）：

| 动作 | 借 | 贷 |
| --- | --- | --- |
| authorize | customer_receivable | merchant_payable |
| capture | merchant_payable；pending_merchant_funds | pending_merchant_funds；refundable_receivable |
| refund | refundable_receivable | customer_receivable |
| void（剩余授权） | merchant_payable | customer_receivable |

- 每个动作内 `Σdebit == Σcredit`；整笔支付的分录汇总也始终借贷相等。
- 恒等式：`customer_receivable 借余 = authorized - refunded - voided
  = refundable + open_authorized`；`refundable_receivable` 贷余 = 可退余额。
- 部分捕获、部分退款、失败重试后，`captured / refunded / refundable`
  与分录汇总始终对得上（见 `tests/test_ledger.py`、`tests/test_persistence.py`）。

## 模拟渠道与故障注入

`app/mock_gateway.py` 是纯本地假渠道。可给它传入脚本（如
`["ok", "raise:timeout", "raise:declined"]`），按调用次数依次返回成功或抛
`timeout/declined/error`，用于验证失败回滚与同 key 重试（见
`tests/test_idempotency.py`）。它不会发起任何网络请求。

## 测试

```bash
uv run pytest -q -s          # 全量，-s 显示每个用例的输入与判定日志
uv run pytest -q             # 只看结果
```

覆盖范围：

- `test_state_machine.py`：全部非法流转，错误码可区分，状态不被静默改动；部分捕获/退款汇总。
- `test_idempotency.py`：顺序重放、同 key 不同内容拒绝、渠道超时/拒绝后同 key 重试。
- `test_concurrency.py`：8 线程同 key 并发只记一次；竞争部分捕获/退款绝不超额。
- `test_ledger.py`：分录平衡与全链路账户恒等式。
- `test_persistence.py`：关闭重开数据库后汇总与分录依旧一致、幂等可重放。
- `test_api.py`：HTTP 状态码/错误体映射、重放字段。
- `test_regression.py`：骨架原 `/` 与 `/health` 端点不回归。

## 项目结构

```
main.py               uvicorn 入口
app/api.py            HTTP 路由与错误码映射
app/models.py         请求/响应模型（Pydantic）
app/service.py        状态机 + 幂等 + 记账的事务编排
app/storage.py         SQLite schema 与连接（事务/PRAGMA）
app/idempotency.py    幂等登记、指纹、重放
app/ledger.py         复式记账分录
app/mock_gateway.py   本地模拟支付渠道（可注入故障）
app/errors.py         业务错误类型（各自带错误码）
tests/                pytest 自动化测试
```

## 边界说明（有意为之）

- 撤销（void）只释放“尚未捕获”的授权；已捕获资金的退回走 refund。
- 不做对账文件导出、通知 webhook、鉴权等；这些超出本地账本范围。
