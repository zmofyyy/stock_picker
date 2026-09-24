# stock_selector · A 股选股 / 回测 / 持续追踪系统

一个可直接运行的 A 股量化工作台：**本地通达信数据读取 → 策略选股 → 组合回测 → 持续追踪 → 报告输出**，
后端 FastAPI + SQLite，前端 React 18 + Vite + TypeScript + Ant Design + ECharts，**全部界面在浏览器中渲染**。

同一套策略代码被「选股 / 回测 / 追踪」三处复用，保证信号口径完全一致；追踪模块支持按日历逐日重建历史状态，
便于验证策略与追踪逻辑。

---

## 目录

1. [功能总览](#一功能总览)
2. [安装](#二安装)
3. [快速开始](#三快速开始)
4. [配置文件](#四配置文件)
5. [数据来源说明（重要）](#五数据来源说明重要)
6. [Web UI 页面说明](#六web-ui-页面说明)
7. [API 一览](#七api-一览)
8. [CLI 一览](#八cli-一览)
9. [项目结构](#九项目结构)
10. [扩展指南](#十扩展指南)
11. [测试](#十一测试)
12. [常见问题](#十二常见问题)

---

## 一、功能总览

| 模块 | 能力 |
| --- | --- |
| 数据 | 通过 `pytdx` 读取本地通达信 `.day` / `.lc5` 文件；缓存到 parquet；支持日期区间过滤、数据质量检查 |
| 选股 | 指定日期或日期区间筛选；输出代码、名称、信号、关键因子；导出 CSV / Markdown |
| 回测 | 多股票日线组合回测；T+1 与成交时点可配；手续费 / 印花税 / 滑点；涨跌停与停牌跳过；12 项绩效指标 + 基准对比 |
| 追踪 | 关注池持久化；11 种股票状态；状态机 + 变迁历史；按日/按小时/手动更新；历史回放；提醒与 Webhook 通知 |
| 报告 | 每日追踪报告 / 区间追踪报告 / 汇总报告，Markdown 与 HTML 双格式，浏览器内渲染 |
| 接口 | FastAPI 提供 REST + WebSocket；CLI 覆盖全部核心功能 |

预置 3 个可配置策略：**双均线交叉**、**RSI 超买超卖**、**放量突破**。

---

## 二、安装

### 1. 环境要求

- Python **3.10+**（已在 Python 3.13 下验证）
- Node.js **18+**（仅前端需要，已在 Node 22 下验证）

### 2. 后端

```bash
cd stock_selector

# 建议使用虚拟环境
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

> **网络较慢时**可使用国内镜像：
> ```bash
> pip install -r requirements.txt -i https://repo.huaweicloud.com/repository/pypi/simple/ --trusted-host repo.huaweicloud.com
> ```

### 3. 前端

```bash
cd frontend
npm install
```

---

## 三、快速开始

### 方式 0：手头没有通达信安装目录？

内置了演示数据生成脚本，会在本地生成一套**格式完全真实的**通达信 `vipdoc` 目录
（含 `sh/sz/bj` 三个市场、日线与分钟线、指数文件），可以直接跑通全部流程：

```bash
python scripts/make_demo_data.py --out demo_tdx --cache-dir ./cache --days 400 --start 2023-01-03

# 用演示配置（tdx_dir 指向 ./demo_tdx，使用独立的 tracking_demo.db）
python cli.py --config config.demo.yaml read-data --tdx-dir ./demo_tdx
python cli.py --config config.demo.yaml screen   --strategy ma_cross --date 2024-07-15
python cli.py --config config.demo.yaml backtest --strategy ma_cross --start 2023-06-01 --end 2024-07-15
python cli.py --config config.demo.yaml track add --code 600000.SH --strategy ma_cross
python cli.py --config config.demo.yaml track update
python cli.py --config config.demo.yaml serve     # 打开 http://127.0.0.1:8000
```

生成的文件与真实通达信的 `.day` / `.lc5` 二进制结构一致，因此**走的就是真实解析路径**
（pytdx 优先 + 内置解析器回退），不是假数据绕过读取层。

> **端口被占用？** 若 `8000` 已被其它程序使用，可改 `config.demo.yaml` 里的 `web.port`，
> 或启动时覆盖：`python cli.py --config config.demo.yaml serve --port 8777`。
> 启动失败时日志会给出明确的端口占用提示，不会静默退出。

### 方式 A：CLI（适合自动化 / 脚本）

```bash
# 1) 查看数据源状态（确认通达信目录是否可用）
python cli.py data-status

# 2) 读取本地通达信数据并写入缓存
python cli.py read-data --tdx-dir C:/new_tdx

# 3) 选股
python cli.py screen --strategy ma_cross --date 2024-01-02

# 4) 回测
python cli.py backtest --strategy ma_cross --start 2020-01-01 --end 2024-01-01

# 5) 加入关注池并追踪
python cli.py track add --code 600000.SH --strategy ma_cross --cost 8.5 --shares 1000
python cli.py track update
python cli.py track report --kind summary --output reports/summary.md
```

### 方式 B：Web UI（适合日常操作）

```bash
# 终端 1：启动后端（默认 http://127.0.0.1:8000）
python cli.py serve

# 终端 2：启动前端开发服务器（http://127.0.0.1:5173，已配置 API 代理）
cd frontend && npm run dev
```

浏览器打开 <http://127.0.0.1:5173> 即可。

也可构建前端后由后端统一提供：

```bash
cd frontend && npm run build      # 产出 frontend/dist
cd .. && python cli.py serve      # 后端自动挂载 frontend/dist
# 访问 http://127.0.0.1:8000
```

---

## 四、配置文件

全部配置集中在根目录 `config.yaml`，命令行参数与 Web UI 提交的参数**优先级更高**。
主要段落：

| 段 | 作用 |
| --- | --- |
| `data` | 通达信目录、缓存目录与格式、起始日期、股票池、复权模式 |
| `strategy` | 默认策略与各策略默认参数 |
| `screen` | 选股最小 K 线数、结果上限 |
| `backtest` | 资金、费率、仓位规则、成交时点、涨跌停、基准、无风险利率 |
| `tracker` | 状态存储、更新时间、状态列表、风险阈值、通知渠道 |
| `web` | 监听地址、端口、CORS、是否挂载前端 |
| `logging` | 日志级别与文件 |

策略参数、追踪参数均**不硬编码**，全部来自此处并可被覆盖。

---

## 五、数据来源说明（重要）

本项目的数据源是**本地通达信安装目录**，读取规则：

```text
{tdx_dir}/vipdoc/{sh|sz|bj}/lday/*.day      # 日线
{tdx_dir}/vipdoc/{sh|sz|bj}/fzline/*.lc5    # 5 / 15 / 30 / 60 分钟线
{tdx_dir}/vipdoc/{sh|sz|bj}/minline/*.lc1   # 1 分钟线
```

- 优先使用 `pytdx.reader.TdxDailyBarReader` / `TdxMinBarReader`；
  当 pytdx 不可用或解析失败时，自动回退到内置的二进制解析逻辑，并在日志与 Web UI 中提示。
- 解析结果统一为 DataFrame，列包含 `datetime, open, high, low, close, volume, amount`。
- 股票代码统一为 `600000.SH` / `000001.SZ` / `830000.BJ` 形式。
- 股票池通过扫描本地目录自动生成，也可用 `data.universe` 指定。

### pytdx 的实际覆盖范围（实测结论）

`pytdx` 内置的 `SECURITY_TYPE` 表只覆盖沪深两市的部分代码段，实测结果：

| 代码 | 板块 | pytdx 是否可直接解析 |
| --- | --- | --- |
| `600xxx.SH` | 沪市主板 | ✅ 可以 |
| `000xxx.SZ` / `300xxx.SZ` | 深市主板 / 创业板 | ✅ 可以 |
| `688xxx.SH` | 科创板 | ❌ 抛 `Unknown security type` |
| `830xxx.BJ` | 北交所 | ❌ 抛 `Unknown security exchange` |

因此本项目**按「pytdx 优先、失败即回退、回退不丢数据」的策略实现**：
科创板与北交所代码会自动落到内置解析器，读取结果与 pytdx 路径**逐字段一致**
（已用同一批 `.day` / `.lc5` 文件做过交叉比对）。回退是**按文件粒度**发生的，
某个市场回退不会影响其它市场，日志中会记录每一次回退及原因。

> **接口差异**：pytdx 两个 reader 的方法名并不统一——
> `TdxDailyBarReader` 提供 `get_df_by_file`，而 `TdxMinBarReader` **没有**该方法，
> 只有 `get_df` / `parse_data_by_file`。读取层会按候选顺序自动探测可用方法，
> 避免因方法名不同而静默退化成内置解析器。

### 成交量 / 成交额单位口径（重要）

两条读取路径对成交量的处理**不一样**，必须分别归一化，否则同一份数据会因走哪条路径而差 100 倍：

| 数据 | 文件内原始单位 | pytdx 返回的单位 | 本项目处理 |
| --- | --- | --- | --- |
| 日线 `.day` | 股 | **手**（pytdx 遵循 zipline 约定做了 ÷100） | ×100 还原为「股」 |
| 分钟线 `.lc1/.lc5` | 股 | 股（**不做**换算，原样返回） | 保持原值 |

归一化后，`volume` 恒为「**股**」、`amount` 恒为「**元**」，
因此「pytdx 读到的沪深主板」与「回退读到的科创板」可以放进同一个回测而口径一致。
`tests/test_tdx_reader.py` 中有对应的回归测试（`test_pytdx_matches_fallback`、
`test_pytdx_matches_fallback_minute`、`test_min_struct_layout_matches_pytdx`）持续守住这条约定。

> 另外注意：分钟线文件里 OHLC 是「**价格 ×100 的 int32**」，不是 float32。
> 内置备用解析器与 pytdx 使用同一布局（`<HHIIIIfII`），写错会导致价格整体错乱。

> ### ⚠️ 关于复权
>
> 通达信本地 `.day` 文件存储的是**不复权（原始成交价）数据**，其中不包含除权除息信息。
>
> - 默认配置 `data.adjust: "none"`，即明确使用不复权数据。
> - 若把 `data.adjust` 设为 `qfq` / `hfq` 却**没有提供除权除息数据**（`data.adjust_file`），
>   系统**不会静默返回错误的复权结果**，而是：
>   1. 在日志中输出 `WARNING`；
>   2. 在 `/api/data/status` 与 Web UI「数据管理」页展示醒目提示；
>   3. 继续按不复权数据处理，并在结果中带 `adjust_warning` 字段。
>
> 需要真实复权时，请提供 CSV 除权除息文件（列：`code,date,cash_dividend,split_ratio`），
> 或改用带复权因子的数据源。**请勿在未提供除权数据的情况下把回测结果当作复权口径结论。**

---

## 六、Web UI 页面说明

| 页面 | 内容 |
| --- | --- |
| **仪表盘** | 数据状态、关注池概况、今日信号、今日状态变更、风险提醒、最新回测绩效摘要 |
| **数据管理** | 设置通达信目录、扫描股票池、读取日线/分钟线、缓存状态与大小、数据更新时间、数据质量检查 |
| **选股** | 选择策略与参数、选择日期与股票池、执行选股、结果表格、一键加入关注池、导出 CSV |
| **回测** | 策略与参数、区间、初始资金、手续费/印花税/滑点、仓位规则、最大持仓数；净值与回撤双图、月度收益、持仓、交易明细、绩效指标；导出 CSV / HTML / Markdown |
| **追踪看板** | 关注池列表（状态、上次状态、变更时间、信号、风险、持仓盈亏）、状态时间线、手动更新、按日期回放、提醒列表、WebSocket 实时刷新 |
| **报告中心** | 每日 / 区间 / 汇总报告，Markdown 浏览器内渲染，复制与导出 Markdown / HTML / CSV |
| **设置** | 通达信目录、数据库路径、策略默认参数、追踪配置、通知配置、定时任务、日志与危险操作 |

所有页面均通过 HTTP API 与后端交互，图表使用 ECharts 在浏览器内渲染，Markdown 使用 `react-markdown` 渲染。
**不存在桌面 GUI，Streamlit 也未作为主 UI。**

---

## 七、API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/data/status` | 数据源状态 |
| POST | `/api/data/load` | 读取并缓存行情 |
| GET | `/api/data/stocks` | 股票池列表 |
| GET | `/api/data/preview/{code}` | 行情预览 |
| POST | `/api/data/quality` | 数据质量检查 |
| GET/POST | `/api/data/tdx-dir` | 读取 / 设置通达信目录 |
| GET/DELETE | `/api/data/cache` | 缓存查询 / 清理 |
| GET | `/api/data/basics` | 股票基础信息（流通股本）查询 |
| POST | `/api/data/basics/template` | 生成 `stock_basic.csv` 模板 |
| POST | `/api/data/basics` | 上传 / 更新流通股本 |
| GET | `/api/screen` | 策略元信息 |
| POST | `/api/screen/run` | 执行选股 |
| POST | `/api/screen/to-watchlist` | 选股结果加入关注池 |
| GET | `/api/backtest/config` | 回测默认配置 |
| POST | `/api/backtest/run` | 执行回测 |
| GET | `/api/backtest/export` | 导出 CSV / HTML / Markdown |
| POST | `/api/backtest/to-watchlist` | 期末持仓导入关注池 |
| GET/POST | `/api/tracker/watchlist` | 关注池查询 / 新增 |
| PATCH/DELETE | `/api/tracker/watchlist/{code}` | 修改 / 删除 |
| POST | `/api/tracker/update` | 追踪更新（可指定日期） |
| POST | `/api/tracker/replay` | 历史回放 |
| GET | `/api/tracker/state/{code}` | 当前状态 |
| GET | `/api/tracker/history/{code}` | 状态变迁历史 |
| GET | `/api/tracker/timeline/{code}` | 状态时间线 |
| GET | `/api/tracker/signals` | 信号记录 |
| GET | `/api/tracker/alerts` | 提醒记录 |
| GET | `/api/tracker/dashboard` | 仪表盘聚合 |
| GET | `/api/report` | 生成追踪报告 |
| GET/POST | `/api/settings` | 读取 / 更新配置 |
| WS | `/ws/tracker` | 追踪状态实时推送 |

完整交互式文档：启动后端后访问 `/docs`（Swagger UI）。

---

## 八、CLI 一览

```bash
python cli.py --help                  # 查看全部命令

# 数据
python cli.py data-status
python cli.py read-data --tdx-dir C:/new_tdx [--freq daily] [--limit 500] [--force]
python cli.py data-quality --max-codes 200
python cli.py cache [--code 600000.SH] [--clear]

# 策略
python cli.py strategies

# 选股
python cli.py screen --strategy rsi --date 2024-01-02 --output picks.csv
python cli.py screen --strategy ma_cross --start 2024-01-01 --end 2024-01-31 --scan-all --format markdown

# 回测
python cli.py backtest --strategy ma_cross --start 2020-01-01 --end 2024-01-01 \
  --cash 1000000 --max-positions 10 --output reports/bt.md --format markdown

# 追踪
python cli.py track add --code 600000.SH --strategy ma_cross --cost 8.5 --shares 1000 --note "观察"
python cli.py track remove --code 600000.SH
python cli.py track list
python cli.py track update                       # 更新到最新交易日
python cli.py track update --date 2024-01-02     # 按日期回放单日
python cli.py track replay --start 2024-01-01 --end 2024-01-31   # 区间回放
python cli.py track state --state 持仓
python cli.py track set-state --code 600000.SH --state 止盈
python cli.py track report --kind daily --date 2024-01-02 --output reports/daily.md
python cli.py track export --output tracking.csv

# 服务与配置
python cli.py serve [--host 0.0.0.0] [--port 8000]
python cli.py settings
python cli.py scheduler --start | --stop | --run
```

---

## 九、项目结构

```text
stock_selector/
├── backend/app/
│   ├── main.py                 # FastAPI 应用、CORS、静态前端挂载、WebSocket
│   ├── api/                    # 路由：data / screen / backtest / tracker / report / settings
│   ├── core/                   # config.py（YAML 配置）、logging.py
│   ├── data/                   # tdx_reader.py（pytdx 优先 + 备用解析）、cache.py、names.py
│   ├── strategies/             # base.py、indicators.py、ma_cross / rsi / volume_breakout、registry.py
│   ├── selector/               # screener.py 选股
│   ├── backtest/               # engine.py、broker.py、performance.py
│   ├── tracker/                # watchlist / state_machine / storage / tracker / notifier / report
│   ├── models/                 # orm.py（SQLAlchemy）、schemas.py（Pydantic）
│   └── services/               # data / screen / backtest / tracker 服务层、container.py 依赖容器
├── frontend/
│   ├── src/
│   │   ├── api/                # client.ts（HTTP 封装）、types.ts
│   │   ├── components/         # EChart / MarkdownView / StateTag / MetricCard / StrategyParamsForm
│   │   ├── pages/              # Dashboard / DataManager / Screener / Backtest / Tracker / Reports / Settings
│   │   ├── App.tsx             # 布局 + hash 路由
│   │   └── main.tsx
│   ├── package.json  vite.config.ts  tsconfig.json  index.html
├── tests/                      # pytest：解析 / 策略 / 回测 / 选股 / 状态机 / 存储 / 提醒 / 回放 / API
├── scripts/
│   ├── make_demo_data.py       # 生成演示用通达信 vipdoc 目录（无通达信环境时使用）
│   └── smoke_e2e.py            # 端到端冒烟：真实启动服务并逐个验证页面与 API
├── cli.py                      # 命令行入口
├── config.yaml                 # 全局配置（模板，tdx_dir 指向真实通达信目录）
├── config.demo.yaml            # 演示配置（配合 ./demo_tdx，零依赖跑通）
├── requirements.txt
├── pytest.ini
└── README.md
```

**SQLite 表**：`watchlist`、`tracking_state`、`tracking_history`、`signals`、`notes`、`alerts`。

---

## 十、扩展指南

### 新增策略

1. 在 `backend/app/strategies/` 下新建文件，继承 `BaseStrategy`：

```python
from .base import BaseStrategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD

class MyStrategy(BaseStrategy):
    name = "my_strategy"
    display_name = "我的策略"
    description = "示例：收盘价上穿 N 日均线买入"

    default_params = {"window": 20}

    factor_columns = ["ma"]

    def generate_signals(self, data):
        df = data.copy()
        df["ma"] = df["close"].rolling(self.params["window"]).mean()
        df["signal"] = SIGNAL_HOLD
        cross_up = (df["close"] > df["ma"]) & (df["close"].shift(1) <= df["ma"].shift(1))
        df.loc[cross_up, "signal"] = SIGNAL_BUY
        df["reason"] = ""
        df.loc[cross_up, "reason"] = "收盘价上穿均线"
        return df
```

2. 在 `registry.py` 中注册；在 `config.yaml` 的 `strategy` 段加入默认参数即可被 CLI / API / Web UI 自动识别。

### 新增股票状态

在 `config.yaml` 的 `tracker.states` 里追加即可；状态机转换规则在 `tracker/state_machine.py`，
按需补充转换与触发条件。

### 接入通知渠道

`tracker/notifier.py` 已内置控制台、CSV、Webhook（钉钉 / 企业微信 / 飞书）、SMTP 邮件；
Webhook 地址与类型在 Web UI「设置 → 追踪与通知」中填写即可。

---

## 十一、测试

### 单元 / 集成测试

```bash
cd stock_selector
pytest -v
```

覆盖范围：

- `.day` 文件解析（含 pytdx 与备用解析路径）
- 四个策略的信号生成与未来函数检查（含「小盘放量」的流通盘过滤与股本解析）
- 回测绩效计算（收益率、最大回撤、夏普、胜率等）
- 选股输出格式
- 追踪状态机合法 / 非法转换
- 追踪记录写入与读取
- 提醒触发条件
- 历史回放一致性
- Web API 基本可用性

### 端到端冒烟（真实启动服务）

`pytest` 用的是进程内测试客户端；下面的脚本会**真的起一个 uvicorn 进程**，
用 HTTP 请求首页、SPA 路由、静态资源与全部主要 API，并回收后端错误日志：

```bash
python scripts/smoke_e2e.py --config config.yaml
```

输出形如：

```text
OK  200 GET  /                                     SPA index.html
OK  200 GET  /Tracker                              SPA index.html
OK  200 GET  /api/data/status                      ok=True
OK  200 POST /api/screen/run                       ok=True  items=3
...
结果：28 项 API + 静态资源全部通过
```

退出码为 0 表示全部通过，非 0 时会列出失败项，便于接入 CI。

---

## 十二、常见问题

**Q：`pytdx` 装不上 / 导入报错？**
A：读取器会自动切换到内置的备用二进制解析逻辑，功能不受影响，日志中会给出提示。
`/api/data/status` 的 `pytdx` 字段会显示可用性（安装了哪个版本、当前会走哪条路径）。

**Q：为什么日志里出现「pytdx 读取失败，回退到内置解析器」？**
A：常见于**科创板（`688xxx.SH`）与北交所（`8xxxxx.BJ`）**——pytdx 的类型表里没有这两个板块，
会抛 `Unknown security type / exchange`。这是预期行为：回退后数据与 pytdx 路径完全一致，
不影响选股、回测与追踪。详见[第五节](#五数据来源说明重要)的覆盖范围表格。

**Q：提示找不到 `vipdoc` 目录？**
A：`tdx_dir` 应指向**包含 `vipdoc` 子目录**的通达信安装目录（如 `C:/new_tdx`），
而不是 `vipdoc` 本身。可在「设定 → 数据」里点「检测目录」验证。

**Q：数据全部是不复权的，回测结果能信吗？**
A：默认口径就是不复权，短周期策略影响较小；跨除权日的选股与回测会失真。
请参考[第五节](#五数据来源说明重要)，提供除权除息 CSV 后再启用 `qfq`/`hfq`。

**Q：回测很慢？**
A：先在「数据管理」页用「股票池数量上限」缩小范围做参数探索，确定后再跑全市场。

**Q：追踪更新报「无数据」？**
A：说明通达信本地数据尚未更新到目标日期。更新通达信盘后数据后重跑
`python cli.py read-data --force`，或在 Web UI「数据管理」页重新读取。

**Q：如何定时自动追踪？**
A：在「设置 → 追踪与通知」中把 `auto_update` 打开并设置 `update_time`（默认 15:30），
点击「启动」即可；也可点「立即执行一次」手动触发。

**Q：前端能改端口吗？**
A：`frontend/vite.config.ts` 中的 `server.port` 决定前端端口，其中已把 `/api` 与 `/ws`
代理到后端 `127.0.0.1:8000`；后端端口在 `config.yaml` 的 `web.port`。

---

## 免责声明

本项目为技术工具，所有策略、指标与回测结果均不构成投资建议。
历史回测表现不代表未来收益，实盘交易风险自负。
