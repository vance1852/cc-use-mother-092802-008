# 分配产品全生命周期的销售费用基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.acceptance
```

验收命令会在临时 SQLite 数据库中登记经营主体、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

## 销售费用承诺与分配系统

`sales_expense/` 在基础服务的同一数据库与审计链上建设费用域，覆盖财务按成熟（mature）、新品（new）、培育期（nurturing）产品分别设定费用目标后的完整闭环：

- **生命周期生效版本**：`exp_lifecycle_versions` 保存不重叠的生效区间，发布新版本取代旧版本；份额在确认时快照 `stage_snapshot/version_id/period_budget_id`，后续切换不追溯改变已确认、已发生费用。
- **年度与期间额度**：按产品 × 年份 × 阶段设置年度额度，再拆到 `YYYYMmm` 月度期间；额度不能下调到已占用/已发生金额以下。
- **活动受益范围与合同承诺**：活动登记品牌/区域/渠道归属和受益产品（因子或声明金额）；承诺创建时立即按可解释规则形成各产品分摊草案，草案不占用额度。
- **可解释分摊**：`allocation.py` 提供因子加权、均分、声明金额三种规则，整数最大余数法保证分毫不差，每行返回权重、化简比例、取整依据与尾差去向。
- **分负责人确认**：只有份额负责人本人能确认自己的份额，确认才按当时生效版本与期间额度占用预算；可在全部确认前对剩余金额重新分摊。
- **发票、应计与跨期**：发票/计提按份额比例回到原承诺；跨期发票支持红字冲回原计提期间（原期间净额为 0，新期间记发生）；退款按原发票归属比例恢复原承诺占用。
- **取消与释放**：取消承诺只释放尚未发生的占用，已发生发票/应计事实保留，关闭后拒绝新增发票。
- **争议部分冻结**：争议可只冻结指定产品份额，其余份额在期间结账中照常处理；冻结份额结账被拒绝，争议解决后恢复。
- **超预算例外**：超额占用必须持有限定金额上限、有效期与 1–3 级授权链的已批准例外；按级别顺序、每级不同人、申请人不得自批、第 3 级必须 admin，例外一次性消耗、过期自动失效。
- **全过程追踪**：`GET /expense/trace?commitment_id=...` 返回申请 → 草案分摊 → 占用（reserved）→ 计提/发票（accrued/occurred）→ 退款（refunded）→ 冻结/解冻 → 结账（settled）→ 释放（released）的逐份额流水与全部事实单据。

### 费用域离线验收

```bash
PYTHONPATH=src python3 -m sales_expense.acceptance
```

### 费用域 HTTP 服务

```bash
PYTHONPATH=src python3 -m sales_expense.api --database expense.sqlite3 --host 127.0.0.1 --port 8090
```

主要路由（均为 POST，写操作带 `request_id` 幂等键与 `X-Actor-Id`）：

| 路由 | 说明 |
| --- | --- |
| `/expense/products`、`/expense/lifecycle-versions` | 产品与生效版本 |
| `/expense/budgets/annual`、`/expense/budgets/periods`、`GET /expense/budgets` | 年度/期间额度与余额 |
| `/expense/campaigns`、`/expense/beneficiaries` | 活动与受益范围 |
| `/expense/commitments`、`/expense/allocations/redistribute` | 承诺与分摊草案/重新分摊 |
| `/expense/shares/confirm`、`/expense/shares/reject`、`/expense/shares/settle` | 份额确认、拒绝、结账 |
| `/expense/invoices`、`/expense/accruals`、`/expense/refunds` | 发票（含跨期冲回）、计提、退款 |
| `/expense/commitments/cancel`、`/expense/periods/settle` | 取消释放、期间批量结账 |
| `/expense/disputes`、`/expense/disputes/resolve` | 争议冻结与解决 |
| `/expense/exceptions`、`/expense/exceptions/decide` | 超预算例外申请与逐级审批 |
| `GET /expense/trace` | 一笔费用的全过程视图 |

金额全部使用整数最小货币单位（分），避免浮点分摊误差。
