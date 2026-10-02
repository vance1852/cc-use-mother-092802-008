# 分配产品全生命周期的销售费用基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

在此基础上，项目同时实现**销售费用承诺与分配系统**：财务按成熟、新品、培育期产品设置差异费用目标后，品牌、区域和渠道团队提交的活动预算可同时服务多个产品；系统保存产品生命周期生效版本、年度与期间额度、活动受益范围、合同承诺、发票与应计事实，按可解释规则生成共享费用分摊草案，业务负责人只能确认自己负责的份额，并支持超预算例外授权链、逐份额争议冻结与一笔费用的全过程追溯。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `fee_service.py`：费用承诺、额度占用、分摊确认、争议结账、例外授权等领域服务；
  - `allocation.py`：共享费用的可解释分摊规则（均分/受益权重/收入基数/固定比例，最大余数法补差）；
  - `fee_acceptance.py`：费用系统的离线端到端验收；
- `tests/`：基础规则、事务边界、接口路由、分摊引擎、费用全流程和端到端验收测试。

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

费用承诺与分配系统的离线验收：

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.fee_acceptance
```

该验收走通生命周期版本与额度建档、多产品共享承诺、可解释分摊草案、逐份额确认、争议仅冻结相关份额、无争议份额先结账、跨期发票、退款回原承诺、超预算例外授权链与全过程时间线。

## 销售费用承诺与分配

### 核心规则

- **生命周期生效版本**：成熟（`mature`）、新品（`new`）、培育期（`nurturing`）按生效日追加版本，永不覆盖；承诺与额度在发生/生效当时快照阶段版本，之后切换生命周期不追溯改变已发生费用。
- **年度与期间额度**：额度同样按生效日版本化，同一版本各期间额度合计不得超过年度额度；占用以「先基础额度、后有效例外授权」的瀑布进行，金额统一使用整数分。
- **活动受益范围与合同承诺**：品牌（`brand`）、区域（`region`）、渠道（`channel`）团队只能登记本团队活动；承诺声明受益产品集合，申请时即按规则形成计划份额并逐产品占用期间额度。
- **可解释分摊**：支持均分、受益权重、收入基数和固定比例（万分比）四种规则，输出每产品金额、基数说明与万分比，尾差用最大余数法补给余数最大（并列时序号最小）的产品，规则、输入与结果全部落审计。
- **份额确认**：分摊草案生成后，产品负责人只能确认自己负责的份额，确认金额不得超过该份额已占用额度。
- **争议冻结**：争议只冻结相关产品份额，其他已确认份额照常结账；解决后份额恢复到争议前状态（草案或已确认）。
- **活动取消、退款与跨期发票**：活动取消只释放未发生部分，已发生费用保留占用继续结账；退款按份额回冲并回到原承诺释放预算；跨期应计/发票只打 `cross_period` 标记，始终锚定原承诺，累计开票净额不得超过累计应计。
- **超预算例外**：例外限定金额上限与有效期；按金额分档逐级授权（经理 → 总监 → CFO），任一级拒绝即终结，到期自动失效并可撤销。
- **全过程追溯**：`GET /commitments/{id}/trace` 返回一笔费用从申请、占用、发生、分摊、确认、结账到释放的份额明细、预算台账与审计时间线。

### HTTP 接口（节选）

写入接口均通过 `X-Actor-Id` 标识操作者并要求 `request_id` 幂等键。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/products`、`/lifecycle-versions` | 产品登记、生命周期生效版本 |
| POST | `/annual-budgets`、`/period-budgets` | 年度/期间额度版本 |
| POST | `/activities`、`/activities/cancel` | 活动受益范围与取消 |
| POST | `/commitments` | 合同承诺与计划份额占用 |
| POST | `/allocations` | 生成/重算可解释分摊草案 |
| POST | `/shares/confirm`、`/shares/dispute`、`/shares/dispute-resolve` | 逐份额确认与争议 |
| POST | `/accruals`、`/invoices`、`/refunds` | 应计、发票（可跨期）与退款事实 |
| POST | `/settlements` | 无争议份额结账 |
| POST | `/exceptions`、`/exceptions/decide`、`/exceptions/revoke` | 超预算例外申请、逐级审批与撤销 |
| GET | `/budget-status`、`/lifecycle-versions` | 额度占用与生命周期版本查询 |
| GET | `/commitments/{id}`、`/commitments/{id}/trace` | 承诺快照与全过程追溯 |

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。
