# 同一账簿 · 日汇率外币报销核销 API

一个计价日、同一本账簿的外币报销核销服务：**FastAPI + SQLite + 精确分数运算**。
在 `/docs` 里按报价号登记 / 改区间 / 撤回正分数闭区间报价；报销单按批提交，
整批判定、原子核销，并留存每个币组当时的**精确折算表**，可按单号回查。

---

## 1. 30 秒启动（docker compose）

```bash
docker compose up --build
# 打开 http://127.0.0.1:8000/docs
```

SQLite 文件在命名卷 `ledger-data` 的 `/data/ledger.db`，计价日可用
`PRICING_DATE` 覆盖：

```bash
PRICING_DATE=2026-11-25 docker compose up --build
```

容器内单 worker 运行 uvicorn（进程锁串行化 + SQLite `BEGIN IMMEDIATE`/主键唯一做持久兜底）。
想在镜像里跑测试：

```bash
docker compose run --rm tests
```

### 本地直接跑（无需 docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
# http://127.0.0.1:8000/docs

pytest                      # 33 passed
python scripts/check_read_scaling.py
```

---

## 2. 需求对照与设计要点

| 需求 | 实现 |
| --- | --- |
| 同一套日汇率核销 | 每个连通币组求一组自洽的精确汇率；不连通币组之间无换算关系，各自一张表 |
| FastAPI 核销 API、SQLite 一个计价日 | 单文件库 `ledger.db`，`meta` 存 `pricing_date`，不做多日期/多账簿 |
| `/docs` 按报价号登记、改区间、撤回 | `POST /quotes`、`PUT /quotes/{no}`、`POST /quotes/{no}/withdraw`（另给 `DELETE` 幂等别名） |
| 最多 32 种币 | `currency.bit` 为 0–31 的位号，币组用整数 `mask` 表示；登记第 33 种币返回 `CURRENCY_LIMIT` |
| 1 原币折目标币的正分数闭区间 | 端点接受 `"7.10"` 十进制或 `"143/20"` 分数；校验 `0 < low ≤ high`，边界含 |
| 报销单按批，单号/两种不同币/原币额/申报额 | `POST /batches`；金额必须为正，原币目标币不同 |
| 十进制正数、按原值核对 | 输入取**原始字符串** → `Decimal` → `Fraction`，全程不用二进制 float；核销后用 `原币额 × 表内精确汇率 == 申报额` 核对 |
| 有效报价 + 已核销单连成币组 | 报价是区间边、已核销单是精确等式边；二者共同做连通分量（报价改动时可合并/分裂，已核销精确边永不消失） |
| 每单须已有换算链 | 提交时两币必须在同一个当前币组，否则 `MISSING_CHAIN` |
| 一套汇率同时满足所有报价、旧单、本批比例 | 精确势并查集 + 乘法最短路闭包（Floyd），把整批等式一起加入后判定 |
| 成功整批核销、留存各组精确折算表、按单号回查 | 一个 `BEGIN IMMEDIATE` 事务写批次/单/快照/汇总；`GET /orders/{no}` 返回该单当时所在组的精确表 |
| 缺链/重号/矛盾说明原因，整批照旧 | 结构化 `reasons[]`（含单号与中文原因），任何驳回都在提交前 `ROLLBACK`，计数不变 |
| 报价与旧单冲突也入档、旧单保留、该组暂停、改正恢复 | 报价始终落库；组置 `suspended` 并写原因，核销返回 `GROUP_SUSPENDED`；`PUT` 改正后重算为 `active` |
| 核销只读所涉组汇总，读入条数不随历史明细增长 | 汇总 JSON 内含精确势 + 全对闭包矩阵（≤32×32）；核销每组只读 **1 行 `group_summary`**，不扫 `ord_item` |
| 报价变动只读写所涉新旧币组 | 仅取两端当前组的成员区域，读该区域有效报价并重算，替换且仅替换这些旧汇总/状态行；有测试验证无关币组 `group_id` 不变 |
| 并发按提交顺序判定，记录与汇总一起生效，失败照旧 | 进程 `RLock` + `BEGIN IMMEDIATE` 全串行，单调 `seq`；业务行与汇总同一事务提交，异常回滚；有并发测试 |

### 数学模型（为什么一份汇总就够）

设 `r_c > 0` 为币 `c` 对某任意基准的价值，只关心比值。

- 报价 `1 S = [L,H] T` ⇔ `L ≤ r_S/r_T ≤ H`。
- 已核销单 `A S → B T` 锁定精确等式 `r_S/r_T = B/A`。

已核销单彼此永远自洽（每批都整体校验通过才入库），因此可用带权并查集把它们压成
**精确势划分**：

```
r_c = mul[c] · y[root[c]]      # 同 root 内比值被精确锁定，mul[root]=1
```

剩下每个精确根一个自由变量 `y_i`。报价被换算成形如 `y_j ≤ U[i][j]·y_i` 的乘法差分约束，
用最小乘积 Floyd 做全对闭包：

```
U[i][j] = “y_j / y_i 的最紧上界”
```

闭包后若某个对角元 `U[i][i] < 1`，即存在乘积小于 1 的矛盾汇率环，系统无解。
新增一批单据 = 加入若干精确等式（两条互为倒数的紧边），再做一次闭包即可判定
**整批是否存在同一套汇率**；可行时令某根 `y=1`、`y_j=U[anchor][j]` 得到一组精确汇率，
并把新合并出的精确势与新闭包写回汇总。全程 `Fraction`，无误差。

---

## 3. 接口一览（详见 `/docs`）

```
POST   /quotes                         登记报价
PUT    /quotes/{quote_no}              改区间（并置为有效；可用于改正后恢复）
POST   /quotes/{quote_no}/withdraw     撤回（留档，币组可能分裂）
DELETE /quotes/{quote_no}              撤回的幂等别名
GET    /quotes?status=active|withdrawn
GET    /quotes/{quote_no}

POST   /batches                        按批提交报销单（整批成败）
GET    /orders/{order_no}              按单号回查 + 当时精确折算表
GET    /batches/{batch_id}             批次与各组快照
GET    /groups                         当前币组/状态/版本
GET    /groups/{group_id}              汇总内部（精确势 + 闭包矩阵）
GET    /health                         健康检查与账本计数
```

驳回响应示例（HTTP 409 / 422）：

```json
{
  "accepted": false,
  "error": "BATCH_REJECTED",
  "reasons": [
    {"code": "CONTRADICTION", "order_no": "BX-2",
     "message": "单据 BX-2 与本批单据 BX-1 的折算比例不能同时满足（单独均可、合批矛盾），整批驳回",
     "extra": {"conflicts_with": ["BX-1"]}}
  ]
}
```

`code` 取值：`MISSING_CHAIN`、`DUPLICATE_ORDER_NO`、`CONTRADICTION`、
`GROUP_SUSPENDED`、`SAME_CURRENCY`、`INVALID_AMOUNT`、`INVALID_CURRENCY`、
`CURRENCY_LIMIT` 等。

---

## 4. 指定试测：单独可行、合批冲突

报价 `USD/CNY = [7.00, 7.20]`（闭区间）。两张单各自都合法：

- `BX-1 100 USD → 710 CNY`（7.10，区间内）
- `BX-2 100 USD → 720 CNY`（7.20，含上端点，单独可核销）

但合一批时，一套汇率不能同时等于 7.10 和 7.20：

```text
POST /batches  {"orders":[BX-1 …710, BX-2 …720]}
HTTP 409  CONTRADICTION
单据 BX-2 与本批单据 BX-1 的折算比例不能同时满足（单独均可、合批矛盾），整批驳回

GET /orders/BX-1 -> 404
GET /orders/BX-2 -> 404        # 整批照旧，计数为 0
```

随后单独提交 `BX-1` 成功，回查得到精确表
`anchor=CNY, USD=71/10, USD/CNY=71/10, CNY/USD=10/71, exact_check=true`。

另有三币三角冲突用例：三单各自落在很宽的报价区间内，但
`USD/CNY=7`、`EUR/USD=1.1`、`EUR/CNY=7.0` 合起来不满足
`EUR/CNY = 1.1×7 = 7.7`，同样整批驳回。测试见
`tests/test_joint_conflict.py`、`tests/test_engine.py`。

### 实测：核销读入条数不随历史增长

`python scripts/check_read_scaling.py`（对 10/100/400/1000 笔历史各做一次探针核销，
埋点统计实际 fetch 行数）：

```text
history_orders | summary calls/rows | state rows | order PK calls/rows
            10 |                1/1 |          2 |                1/0
           100 |                1/1 |          2 |                1/0
           400 |                1/1 |          2 |                1/0
          1000 |                1/1 |          2 |                1/0
successful probe group_summary read: {'calls': 1, 'rows': 1}
RESULT: PASS
```

即每个所涉币组恒定只读 1 行 `group_summary`、2 行 `currency_state`，
单号查重是 1 次主键点查（新单号 miss，fetch 0 行）；没有任何随 `ord_item`
历史增长的读取。

### 实测：测试与真实 HTTP

- `.venv/bin/python -m pytest` → **33 passed**（含引擎数学、API 全流程、
  指定合批冲突、原子回滚、32 币上限、合并/分裂、并发三个用例、
  80 组随机“真汇率世界”性质测试）。
- 另跑过 300 组随机性质脚本（顺序多批 + 汇总 JSON 往返 + 恶意偏离单必被拒），0 失败。
- 用 `uvicorn app.main:app` 真实起服务，`curl` 验证 `/docs`、`/openapi.json` 200，
  以及上述报价→合批冲突→整批不存在→正常核销→按单号回查精确表的完整链路。
- `/tmp/docker-compose config` 校验 `docker-compose.yml` 通过。

---

## 5. 代码结构

```text
app/
  exact.py     # Decimal/字符串 -> Fraction，分数序列化与近似显示
  engine.py    # 带权 DSU 精确势 + 乘法 Floyd 闭包；纯函数、不碰 DB
  db.py        # SQLite、BEGIN IMMEDIATE 事务、读行埋点
  schemas.py   # Pydantic 模型（数字输入也兼容，统一转精确文本）
  service.py   # 报价生命周期、区域重算分组、整批裁决与原子核销、回查
  main.py      # FastAPI 路由与异常处理
tests/         # 33 个测试
scripts/check_read_scaling.py
Dockerfile  docker-compose.yml  requirements.txt  pytest.ini
```

---

## 6. 缺项与如实说明

1. **本沙箱没有 docker / daemon**，因此镜像没有在这里真正 `docker compose up --build`
   跑过；已做到的是：依赖在同版本 Python 3.11 / linux x86_64 虚拟环境中全部实测、
   uvicorn 真实启动并 curl 通过、`docker compose config` 校验通过。Dockerfile 仅用标准
   `python:3.11-slim` + `pip install -r requirements.txt`，锁定版本均为已验证版本。
2. **精确相等语义**：按题面“按原值核对 / 精确折算比例”，申报额必须能被某套精确汇率
   严格满足，不设“分位四舍五入容差”。真实报销常有凑整，若需要可加显式舍入/容差策略，
   但那会改变“按原值”的严格含义，故未擅自加入。
3. **并发顺序**：单 worker 下以进程锁按获取锁顺序（即 `seq` 裁决顺序）全串行；
   多 worker 时进程锁不跨进程，但 `BEGIN IMMEDIATE` 与 `ord_item.order_no` 主键仍保证
   可串行与无重号，最终顺序以 SQLite 写锁授予顺序为准，而非 TCP 到达的精确时刻。
4. **报价改动会生成新的 `group_id`**（合并/分裂后身份更自然）；历史单保留成交时的
   `group_id` 与独立 `snapshot_id`，因此报价后续改动不影响按单号回查当时的精确表。
5. 无鉴权、无分页（单日、≤32 币；单号/批次均为点查，`GET /quotes` 数据量为报价号数量级）、
   无多账簿/多计价日，这些都超出题设范围。
6. 强烈建议金额/区间用**字符串**提交；直接发 JSON number 也能兼容（按最短 repr 还原常见
   十进制字面量），但二进制 float 若在客户端已做过运算，原始精度无法在服务端复原。
