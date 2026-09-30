# Payment Ledger Service（本地模拟版）

一个纯本地、可直接运行的 FastAPI 支付账本服务。用**模拟支付渠道**管理一笔支付从
**授权（authorize）→ 捕获（capture，支持多次部分捕获）→ 退款（refund，支持多次
部分退款）/ 撤销（void）** 的完整链路，并为每一次成功的金额变动记一套**平衡的
复式分录**。

不依赖任何真实支付渠道、外部服务或凭据；数据保存在进程内存中。

---

## 1. 快速开始

```bash
# 安装依赖（uv）
uv sync

# 启动本地服务
uv run uvicorn main:app --host 127.0.0.1 --port 8000
# 或： uv run python main.py
```

服务启动后：

- 健康检查：`GET /health`
- 交互式 API 文档：`http://127.0.0.1:8000/docs`
- OpenAPI：`http://127.0.0.1:8000/openapi.json`

### 端到端示例

```bash
BASE=http://127.0.0.1:8000

# 1) 授权 100.00 USD
curl -s -X POST $BASE/v1/payments/authorize -H 'Content-Type: application/json' \
  -d '{"amount":"100.00","currency":"USD","idempotency_key":"a1"}'
# -> payment_id, status=AUTHORIZED

# 2) 第一次部分捕获 30.00（可多次）
curl -s -X POST $BASE/v1/payments/$PID/capture -H 'Content-Type: application/json' \
  -d '{"amount":"30.00","currency":"USD","idempotency_key":"c1"}'
# -> status=PARTIALLY_CAPTURED

# 3) 部分退款 10.00（可多次，不能超过可退余额）
curl -s -X POST $BASE/v1/payments/$PID/refund -H 'Content-Type: application/json' \
  -d '{"amount":"10.00","currency":"USD","idempotency_key":"r1"}'

# 4) 查询（含状态、金额、分录与科目净额）
curl -s $BASE/v1/payments/$PID
```

撤销（未发生任何捕获时才允许）：

```bash
curl -s -X POST $BASE/v1/payments/$PID/void -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"v1"}'
```

### HTTP 接口

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/v1/payments/authorize` | 授权，body: `amount, currency, idempotency_key` |
| POST | `/v1/payments/{id}/capture` | （部分）捕获 |
| POST | `/v1/payments/{id}/refund` | （部分）退款 |
| POST | `/v1/payments/{id}/void` | 撤销授权 |
| GET  | `/v1/payments/{id}` | 查询状态、金额、操作流水、科目净额 |

每个变更接口的成功响应形如：

```json
{
  "replayed": false,
  "operation": { "type": "CAPTURE", "amount": 3000, "gateway_ref": "GW-0000000002", "...": "..." },
  "payment": {
    "status": "PARTIALLY_CAPTURED",
    "authorized_amount": 10000, "captured_amount": 3000,
    "refunded_amount": 0, "refundable_amount": 3000,
    "account_balances": { "CUSTOMER_CLEARING": 3000, "...": 0 }
  }
}
```

---

## 2. 状态流转规则

状态（`PaymentStatus`）：

| 状态 | 含义 |
| ---- | ---- |
| `AUTHORIZED` | 授权成功，尚未捕获 |
| `PARTIALLY_CAPTURED` | `0 < 已捕获 < 授权额`，未退款 |
| `CAPTURED` | `已捕获 == 授权额`，未退款（已全额结算捕获） |
| `PARTIALLY_REFUNDED` | `0 < 已退款 < 已捕获` |
| `REFUNDED` | `已退款 == 已捕获 > 0`（全额退完） |
| `VOIDED` | 授权被撤销，且没有任何捕获 |
| `FAILED` | 授权被渠道拒绝（当前模拟渠道默认不产生此状态，预留给后续扩展） |

合法流转：

```
                 capture 部分
  AUTHORIZED ───────────────────► PARTIALLY_CAPTURED
      │  capture 全额 (=授权额)        │ capture 到授权额
      │            └──────┬───────────┘
      ▼                   ▼
   CAPTURED ◄─────────────┘
      │  refund 部分
      ▼
 PARTIALLY_REFUNDED ──refund 到已捕获额──► REFUNDED

  AUTHORIZED（且 captured=0）── void ──► VOIDED
```

关键约束（违反即拒绝，错误码各不相同，绝不静默改状态）：

- **只有未捕获的授权可以撤销**；已有捕获后撤销 → `ALREADY_CAPTURED`（应走退款）。
- **已撤销后不能再捕获 / 退款 / 再撤销** → `ALREADY_VOIDED`。
- **捕获金额必须 > 0**，且累计捕获不能超过授权额：
  - 金额 ≤ 0 → `CAPTURE_AMOUNT_NOT_POSITIVE`
  - 超额 → `CAPTURE_EXCEEDS_AUTHORIZED`
  - 已全额捕获后再捕获 → `PAYMENT_FULLY_SETTLED`
- **一旦发生过退款，不允许再捕获**（退款不能用「再捕获」补上可退余额）→
  `INVALID_STATE_TRANSITION`。
- **退款金额必须 > 0**（`REFUND_AMOUNT_NOT_POSITIVE`），且不能超过
  `可退余额 = 已捕获 - 已退款`；没有可退资金或超额 → `REFUND_EXCEEDS_REFUNDABLE`。
- 币种与支付币种不一致 → `CURRENCY_MISMATCH`；授权金额非正 → `INVALID_AMOUNT`。
- 支付不存在 → `PAYMENT_NOT_FOUND`（HTTP 404）。

金额恒等式（每次成功变更后都在服务内强制校验，不一致直接抛错）：

```
0 <= refunded_amount <= captured_amount <= authorized_amount
refundable_amount = captured_amount - refunded_amount
```

---

## 3. 金额与币种约定

- **金额一律以整数「最小货币单位」存储和计算**（如 USD 的分、EUR 的分、CNY 的分；
  JPY 为 0 位小数，最小单位即 1 日元）。代码路径上没有任何 float 参与金额运算。
- 入站的十进制金额（JSON 数字或字符串）经 `Decimal(str(amount))` 转换，并按币种
  小数位以 **ROUND_HALF_UP** 取整，避免 `0.1 + 0.2` 这类二进制浮点误差污染账目。
- 响应同时给出最小单位整数（`*_amount`）和面向人的定小数字符串（`*_display`）。
- 支持币种：`USD`、`EUR`、`CNY`（2 位小数）与 `JPY`（0 位小数）；币种代码大小写
  不敏感（统一转大写），未知币种直接拒绝。
- 一笔支付绑定一个币种，捕获/退款的币种必须与授权一致，否则 `CURRENCY_MISMATCH`。

### 复式记账科目

| 科目 | 方向含义 |
| ---- | -------- |
| `AUTHORIZED_FUNDS` / `AUTHORIZED_RESERVE` | 授权备忘科目对（表外），借/贷记录已授权但未捕获的资金 |
| `CUSTOMER_CLEARING` | 渠道待结算款（资产），捕获借记、退款贷记 |
| `MERCHANT_PAYABLE` | 应付商户款（负债），捕获贷记 |
| `REFUND_CLEARING` | 退款清算，退款借记，与待结算款对冲 |

- 每一次操作的分录**借贷必相等**（授权/撤销是备忘对，捕获是「真实分录 + 释放对应
  授权备忘」，退款冲减待结算款）。
- 任意时点科目净额恒等于汇总数：
  - `CUSTOMER_CLEARING` 净借 = 已捕获 − 已退款 = 可退余额
  - `MERCHANT_PAYABLE` 净贷 = 已捕获
  - `REFUND_CLEARING` 净借 = 已退款
  - 备忘科目净借/净贷 = 授权额 − 已捕获（撤销后为 0）

---

## 4. 幂等规则

每个会改变金额或状态的接口都必须带 `idempotency_key`（非空字符串）。

1. **同键同内容重放**（包括并发重复提交）：服务端返回**第一次成功时缓存的同一响应**
   （`replayed: true`），只记一次账、只扣一次可退余额、只有一条操作流水和一个
   渠道参考号 `gateway_ref`。
2. **同键不同内容**（金额、币种、操作类型、目标支付任一不同）：明确拒绝，错误码
   `IDEMPOTENCY_CONFLICT`（HTTP 409），不会用新内容覆盖旧记录。
3. **并发安全**：所有变更操作在同一把进程内锁（`store.lock`）的临界区里原子完成
   「幂等键检查 → 业务校验 → 调用模拟渠道 → 写账本 → 缓存响应」。并发的相同请求
   被串行化：第一个执行，其余全部命中缓存重放。
4. **失败可安全重试**：模拟渠道调用先于本地记账。渠道瞬时故障（`GATEWAY_FAILURE`，
   HTTP 502）时本地不写任何状态、不缓存结果；用**同一个幂等键**重试即可，渠道侧
   也以该键作为客户端请求 ID 去重，因此失败重试不会重复记账。

请求指纹是规范化 JSON（`json.dumps(..., sort_keys=True)`），金额用最小单位整数，
所以 `"100.00"`、`100`、`1e2` 这类等价写法指纹相同。

### 故障注入（仅用于本地/测试）

`PaymentService.gateway.fail_once` 是一个集合，放入幂等键后，该操作的下一次调用
会模拟渠道瞬时失败一次、再下一次成功，用于验证失败重试路径。

---

## 5. 本地验证

```bash
# 全量自动化测试（日志默认输出输入与判定结果）
uv run pytest

# 只看重点场景
uv run pytest tests/test_concurrency.py   # 并发重复提交、竞争
uv run pytest tests/test_idempotency.py  # 幂等重放/冲突/失败重试
uv run pytest tests/test_service_lifecycle.py  # 非法流转、部分捕获/退款、账目平衡
uv run pytest tests/test_api.py          # HTTP 端到端

# 安静模式
uv run pytest -q
```

测试覆盖：

- 正常授权/部分捕获/全额捕获/部分退款/全额退款/撤销全链路；
- 重复提交（顺序重放）与**并发重复提交**（16 线程同键捕获/退款只记一次；8 个不同键
  并发部分捕获时恰好 5 个成功、其余以可区分的错误码拒绝、总额不超授权）；
- 撤销与捕获竞争时只有一方成功；
- 全部非法状态流转与超额退款/捕获，且错误码彼此可区分；
- 幂等键内容冲突（改金额/改币种/换操作）被 409 拒绝；
- 渠道故障后同键重试成功且不重复记账；
- 每笔分录借贷平衡、科目净额与「已捕获/已退款/可退余额」始终对得上；
- 原工程的 `main:app` 可创建、可服务（见 `tests/test_smoke.py`）。

## 6. 目录结构

```
main.py                  # ASGI 入口（保持原 bootstrap 的 app 暴露方式）
conftest.py              # 测试日志配置
app/
  errors.py              # 领域错误与稳定错误码
  money.py               # 币种与整数最小单位金额处理
  models.py              # 状态枚举、Payment/Operation/Entry 模型
  gateway.py             # 模拟支付渠道（操作级去重 + fail_once 故障注入）
  store.py               # 带锁内存存储 + 幂等索引
  ledger.py              # 复式分录构造与平衡校验
  service.py             # 状态机、幂等编排、金额/科目不变量校验
  api.py                 # FastAPI 路由与错误码 -> HTTP 状态映射
tests/                   # 自动化测试
```

## 7. 已知边界

- 存储为进程内内存，重启即清空；不涉及真实持久化与跨进程部署（单进程内并发安全）。
- 模拟渠道默认总是批准授权；`FAILED` 状态为后续扩展预留。
