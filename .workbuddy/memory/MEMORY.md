# MEMORY.md — stock_rsi 工作区长期记忆

## 项目清单（同一工作区内的独立项目）

| 目录 | 说明 | 端口 |
|---|---|---|
| `stock_selector/` | 早期主力项目：选股/回测/追踪全栈（React+Vite 前端，276 项 pytest） | 8000（被 `stock_blocks` 占用）→ 演示用 8777 |
| `stock_picker/` | 放量选股工具：本地 `.day` → 放量筛选 → K 线 → 计划买入/卖出价 → 追踪 | **8778** |

`F:\source\stock_blocks\stock_selector` 是**兄弟项目**，长期占用 `127.0.0.1:8000`，
不要动它的进程。

## 数据目录（HOME）——两套，别搞混（2026-09-23 定）

`config.py` 里 `APP_DIR` 决定可写目录，**计划/追踪跟着 HOME 走**：

| 启动方式 | HOME | `data/stock_picker.db` 内容 |
|---|---|---|
| `dist/stock_picker.exe`（双击/默认） | `dist/` | **用户的真实数据**（2026-09-23 实测 plans 3 条 + watchlist 1 条） |
| `python -m stock_picker.app`（源码态） | `stock_picker/` | 单独的库，2026-09-23 实测为空 |

- 用户在 8778 上两种都跑过；**源码态看不到 exe 里的计划，不是数据丢失**。
  要共用得设 `STOCK_PICKER_HOME=dist`。
- 用户数据锚点（记下来，别当垃圾清）：plans `603686.SH 福龙马`(id12)、
  `600360.SH 华微电子`(id13)、`605228.SH XD神通科`(id14)；watchlist `600360.SH`(id12)。
- `dist/data/backups/20260922-2317{05,18}*.db` 里的「华纺股份」(id15/16) 是**测试残留**，不是用户的。

## 本机环境（固定事实）

- 真实通达信目录：**`D:\new_tdx`**（`vipdoc/sh/lday` 4935 + `sz/lday` 4449 + `bj/lday` 348）
- A 股正股 **~5605 只**（沪 600/601/603/605/688/689；深 000/001/002/003/300/301；京 43/83/87/88/92）。
  **这个数字会随上市/退市变化，任何脚本里都不要写死**（2026-09-21 是 5602，09-23 已是 5605）。
- Python：`C:\Users\Ming\.workbuddy\binaries\python\versions\3.13.12\python.exe`
  （fastapi / uvicorn / pandas / numpy / pyarrow / pytdx / tqdm 齐全）
- Bash 工具需先补 PATH：
  `export PATH="/c/Users/Ming/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:$PATH"`
- PowerShell 工具**不回显 stdout**，要重定向到文件再读

## 通达信数据格式（踩过坑，勿忘）

### `.day` 日线（定长 32 字节）
`<IIIIIfII`：date/open/high/low/close(uint32, 单位「分」) amount(float32, 元)
volume(uint32, **股**) prev_close(uint32, **已失效，别用**)

### 量纲差异
- 文件原始值：价格「分」、成交量「**股**」
- pytdx 返回：价格已 ÷100（元）、成交量**额外 ÷100**（「手」）→ 需 ×100 还原
- pytdx 行索引名是 `date`（不是 `datetime`）
- pytdx 不支持 **688 科创板 / 8xxxxx 北交所**，抛 `Unknown security exchange`，
  必须按文件粒度回退

### 性能结论
`.day` 可直接 `np.frombuffer` 向量化映射，与 pytdx **逐字段完全一致**，
但快 117 倍（全市场 60s → 8s）。优先用向量化做批量扫描，pytdx 用于单只读取与自检。

### 概念板块（2026-09-21 破解结果）

两个文件，**互相印证**，都是 GBK 文本：

| 文件 | 内容 | 格式 |
|---|---|---|
| `T0002/hq_cache/infoharbor_block.dat` | 板块 + **成分股**（742 KB） | 板块头 `#<前缀>_<板块名>,<声明成员数>,<指数代码>,<起始日>,<更新日>,,`；成分行 `<市场>#<代码>`（0 深/1 沪/2 京），逗号分隔 |
| `T0002/hq_cache/tdxzs.cfg` | 板块指数表 | `板块名\|880xxx\|类型码\|?\|0\|显示名` |

- 前缀三类：`GN_` 概念（269）、`FG_` 风格（161）、`ZS_` 指数（117）
- 类型码：**4=概念**（269，与 `GN_` 数量吻合）、5=风格、3=地区、2=行业
- **命名口径不同**：block 用简称（「锂电池」「智能机器」），cfg 用全称
  （「锂电池概念」「机器人概念」），36 个概念有差异 → **交叉验证必须按板块指数代码**，
  不能按名字。按指数代码求交集是 **269/269 双向零残差**
- 结果：269 概念 / 46529 条归属 / 平均每股 8.55 个（最多 49）/ A 股覆盖 **97.02%**
- `tdxbk.cfg` 只有几十条自选概念（1.4 KB），**没有成分股，别用它**
- 通达信把「含H股」「次新股」「ST板块」这类**属性标记**也算概念，原样保留不筛选

### `T0002/hq_cache/*.tnf` 名称表格式（破解结果）
- 文件 = N×360 字节记录 + 50 字节尾部；记录锚点自偏移 0
- 名称：记录内 `[81:89]`（8 字节 GBK）
- 代码：名称前 40 字节窗口内最后一段连续 6 位数字
- 对本地 vipdoc 覆盖率约 97%

### 行业分类（三套，都在 `incon.dat` 分段）

| 段 | 体系 | 码 | 股票归属来源 |
|---|---|---|---|
| `#TDXNHY` | 通达信行业 | `T`+2/4/6 | `tdxhy.cfg` 第 3 字段 |
| `#TDXRSHY` | 通达信研究行业 | `X`+2/4/6 | `tdxhy.cfg` 第 6 字段 |
| `#SWHY` | 申万行业 | 6 位数字 | **本地无归属文件，不可用** |

`tdxhy.cfg`（GBK，6 字段）：`市场|代码|T码|||X码`，市场 `0`深 `1`沪 `2`京。
`#TDXRSHY` 按长度分级：一级=`X`+2(30 个)、二级=`X`+4(128 个)、三级=`X`+6(316 个)。
本项目用 **X 码（研究行业）**，二级=前 5 字符，A 股覆盖 99.3%。

### 代码前缀 → 交易所（易错）
**`92xxxx`（如 `920427`）是北交所**，本机 `vipdoc/bj/lday` 全部是 92 段。
`_guess_market()` 必须把 `92` 前缀判定放在「`9` 开头 = 沪市」**之前**，
否则北交所个股会被判成 SH 而读不到文件。

## 涨跌停判定与连板梯队（2026-09-23 新增，参照兄弟应用 stock_watch/views/streaks.py）

- **连板梯队**在 `stock_picker` 侧栏第 2 项；后端 `service.streaks()` + `GET /api/streaks`，
  核心判定独立成 **`stock_picker/limits.py`**；专项回归 `packaging/streak_test.py`（64 项）。
- **`.day` 第 8 个 uint32（上日收盘）已失效**：自 **2015-12-16** 起全市场失效
  （沪/深约 63~67% 是占位值 `65536`、约 10% 是 `0`；北交所从来没有）。
  → **不落缓存列**（否则多 68MB），基准价一律用「**前一根 K 线收盘**」
  （也是 stock_watch 在用的口径）。`tdx_reader` 里只留 `prev_close_from_raw()` 供自检。
- 涨停价 = `(prev_c * f + 50) // 100`，**「分」整数**，`f` = 涨跌幅 ×100
  （主板 110 / 创业·科创 120 / 北交所 130）。**上市首日剔除**（`is_ipo`）。
- **⚠️ 本机行情里 ST 股的带宽也是 10%，不是 5%**（2026-09-23 实测）：
  144 只名称含 ST 的主板股中 **141 只（97.9%）**出现过「最高价 > 前收 × 1.05」，
  占其交易日 8%~37%，`high/前收` 的 p99.9 **≈ 1.10**。
  兄弟应用 caption 也写「**ST 股按常规板近似**」。
  → **`st_limit` 默认关闭**（service / app / 前端勾选框三处）。
  开着的代价：`2026-06-01` 从 120 只变 168 只。
  **名称表里的「ST」前缀 ≠ 5% 带宽 —— 判涨跌幅限制要看行情本身，不能只信名称。**
- 封板形态：一字板（最低价 = 涨停价）/ T字板（开 ≥ 涨停价但盘中打开）/ 换手板。
- 连板计数全向量化：`(n_cat, window)` 右对齐矩阵展平；`boundary = (~lu) | (pos == 0)`；
  `streak_all = np.where(boundary, 0, gp - np.maximum.accumulate(np.where(boundary, gp, -1)))`。
  实测 **0.05~0.16s**（含 2015/2018/2022 历史日期）。窗口 `STREAK_WINDOW = 60`。
- **`as_multi()`：列表参数必须挡住裸字符串**。`[b for b in "主板"]` → `['主','板']`，
  然后静默匹配不到任何板块、**返回空结果不报错**。未知板块/未知交易所现在都明确报错。
- 交叉核对基准：`F:\source\stock_blocks\stock_watch\data\universe.parquet`
  （含现成 `limit_up` 列）。默认口径下 8 个交易日**逐只零差异**。

## 编码约定

- 行情「涨为红、跌为绿」（中国习惯），货币 ¥
- 均量基准用**前 N 日**（不含当日），避免未来函数
- 任何「按每只股票自己的最后一根 K 线」滑动的逻辑，都要改以**全市场交易日**为锚，
  否则停牌/退市股会冒充当期信号
- **选股默认只保留主板**：科创板（688/689）、创业板（300/301）、北交所（43/83/87/88/92）
  全部默认剔除（用户 2026-09-21 分两次明确要求）。
  板块判定统一用 `tdx_reader.board_of()`，
  可选板块顺序为 `("主板","创业板","科创板","北交所")`。
  白名单参数 `boards` 显式传空列表时**报错**，不静默回退默认值

## stock_picker 前端约定（2026-09-21 定）

- **视觉风格：macOS Vibrancy**。三档深灰 `#1c1c1e`→`#2c2c2e`→`#3a3a3c`；
  1px 半透明描边只用 `rgba(255,255,255,.08)`/`.12`；侧栏与抽屉用
  `backdrop-filter: blur(28~30px) saturate(180%)` + 半透明底；
  标题 Georgia 衬线 / 正文系统无衬线 / 数字 SF Mono；
  过渡 `200ms` 且**只过渡颜色**；**无渐变、无光晕、无 pulse/bounce**。
  涨跌用 macOS 深色系统色：涨 `#ff453a`、跌 `#30d158`。
- **选股表不设操作列**：点整行开抽屉（抽屉内含 保存计划/加入计划/加入追踪）。
  目的是让核心列（含买入价、卖出价输入框、概念）在 1258px 视口下**一屏装下**。
  改列时务必保持 `#screenTable` 的 `scrollWidth <= .table-wrap` 的 `clientWidth`；
  加「概念」列后实测自然宽 1068px（概念列 165px），`min-width` 设 1100px 留 30px 余量。
- **分布卡（行业 / 概念）的基数必须独立于筛选链**：`summary_base` = 「除行业/概念筛选本身
  以外的全部条件命中集」，在过滤**之前**取快照。曾把它放在过滤之后统计，导致点选一个行业后
  分布卡只剩那一个行业、其余 chip 全消失。三个计数语义不同，别混用：
  `summary_base`（除行业/概念外全部条件命中）< `total_hits`（叠加行业/概念后）
  < `matched`（再经 max_results 截断）。回归脚本：`packaging/summary_base_test.py`。
- K 线图：买卖价用 markLine 虚线（蓝=买入、红=卖出）+ 右上角 `graphic` 文字，
  **不要**把文字挂在 markLine 的 label 上（会与 y 轴刻度、放量 pin 打架）。
  y 轴 `min/max` 需把买卖价纳入范围，且返回值要 `toFixed(3)` 掐掉浮点尾巴，
  否则刻度会渲染成 `4.140000000000001`。
- **K 线抽屉宽度 `min(1440px, 95vw)`**（2026-09-22 由 1040px 加宽），图 `.chart.tall` 高 620px。
  `#klineChart` **必须是 `.drawer-body` 的直接子元素** —— 曾因 `.drawer-body` 里多一个 `</div>`，
  图表被解析成 `.drawer` 的子元素，导致 `.drawer-body` 只剩 40px 高、顶部买卖价整行被压扁、
  图高被 flex 从 620 压到 440，而页面照样能打开（沉默失败）。
  改完抽屉布局务必查 `#klineChart.parentElement.className`；冒烟测试已加硬断言。
- 布局：`body` 滚动 + `.sidebar` `position: fixed` + `.toolbar` `sticky`。
  不用「fixed 内部滚动」写法，否则整页截图与打印会截不全。
- **连板梯队的行业/概念筛选走本地**（`streakFiltered()`），不重新请求后端 ——
  与选股页不同（那边每次筛选都重新 `/api/screen`）。所以点 chip 是瞬时的，
  且分布 chip 数量天然不受影响。
- 连板明细表 12 列，实测自然宽 **970px**、`scrollWidth - wrap.clientWidth = 0`（viewport 1258px）。
  改成 13 列以上要重新量一遍。
- 排序逻辑已抽成通用件：`orderBy(rows, sort, cols)` / `paintSortHeaders(sel, sort)` /
  `bindSortHead(sel, sort, cols, onChange)`。选股页与连板页共用，**别再各写一套**。
- 连板页切过去时才首次加载（`switchTab`），已有数据则只 `resize()` 图表。

## 环境技巧：让服务跨工具调用存活（2026-09-23 发现）

以前记的「前台 Bash 里起的服务会在调用结束时被按进程组回收，必须把『起服务+测试+收尾』
塞进同一次调用」**有一个更省事的例外** —— 用 PowerShell 的 `Start-Process`：

```powershell
Start-Process -FilePath "F:\source\stock_rsi\dist\stock_picker.exe" `
  -ArgumentList '--no-browser','--port','8778' -WorkingDirectory "F:\source\stock_rsi\dist"
```

独立进程树，后续多次调用都能访问，收尾 `taskkill //F //IM stock_picker.exe`。
好处：`agent-browser` 的浏览器自动化可以分多步慢慢做（open → 点页签 → eval 读数 →
截图 → close），不再挤在一次调用里。注意 `agent-browser` **没有 `resize` 命令**，视口改不了。

## 前端资源指纹与缓存（2026-09-23 定，反复踩过）

**症状**：改了 `static/` 也重新打包了，用户说「怎么还是老样子」。
**真相**：后端 100% 正确，是浏览器里跑着旧 `app.js`。踩过两次的形态是
「日期控件下界还卡在 `?n=250` 的 2025-09-12」—— 服务重启后端口不变、旧标签页
照常调接口，从表现上完全看不出前端是旧的。

三道防线（`app.py` + `index.html` + `app.js`），缺一道就可能复发：

| 防线 | 做法 |
|---|---|
| 响应头 | `@app.middleware("http")`：`/static/*` → `no-cache, must-revalidate`；`/` 与 `/api/*` → `no-store`。**GET 接口不加 `Cache-Control` 会被浏览器启发式缓存** |
| URL 指纹 | `ASSET_VERSION = md5(app.js+style.css+index.html)[:10]`；`/` 必须用 `HTMLResponse` 替换 `__ASSET_VER__`（**不能再走 `FileResponse`**），页面请求 `/static/app.js?v=<指纹>` |
| 页面自检 | `GET /api/version` + `app.js::loadVersion()`：加载时记指纹，每 20s 与每次 `visibilitychange` 比对，不一致显示 `#staleBanner` 橙色横幅 |

**排查铁律**：遇到「改了前端没生效」，先怀疑浏览器再怀疑后端：
① `curl` 接口看返回值 → ② `curl` 静态资源与源码 `diff` → ③ 干净会话浏览器读 DOM 真实状态。
`agent-browser` 的 `open`/`eval`/`close` 要**塞进同一次 Bash 调用**（和起服务同理），
冷启动可能要 1~3 分钟，`timeout` 给到 200s。

## 启动即预加载（2026-09-23 用户要求「加载所有存在数据」）

- 服务 `@app.on_event("startup")` 起**后台线程**跑 `MarketService.preload()`，
  端口先就绪、页面先打开：名称表 → 行业 → 概念 → 流通股本 → 行情缓存 → 内存行情 → **交易日索引**
  （最后一步原为「均量预热」，2026-09-23 改为「交易日索引」—— 均量已不落列、无需预热）。
- **缓存过期判定**：`bars.parquet` mtime vs `vipdoc/*/lday` 下最新 `.day` 的 mtime
  （`os.scandir` + `DirEntry.stat()`，9741 文件 30ms；**不要 fopen 全部文件去比内容**），
  **外加口径比对**（见下节）。过期才重建；已最新时整轮预加载 **0.3~0.6s**。
- **重建必须拿 `self._refresh_lock`，并在锁内重判 freshness**。
  `refresh()` 拆成 `refresh()`（拿锁）与 `_refresh_impl()`（干活）；
  `preload()` 内部**不能**直接调 `_refresh_impl` 绕锁 —— 会和手点「刷新数据」
  同时写同一个 `bars.parquet.tmp`。
- 配置 `config.json → bootstrap: {preload, refresh_if_stale, auto_screen}`（默认全 true）。
  两个现有 config.json 里都还没写这一段，由 DEFAULTS 兜底合并，属正常。
- 接口：`GET /api/bootstrap`（`running`/`ready`/`steps`/`freshness`/`settings`）、
  `POST /api/preload`。**`ready` = 不在跑（失败也算 ready）**，否则一个挂掉的预加载会把前端卡死。
- 前端 `waitBootstrap()`：运行中显示进度条，完成后 toast，然后才 loadStatus/…/`doScreen()`；
  再预取 `loadPlans()/loadWatch()`（切页即见）。

## 行情缓存口径：全历史 + dtype 收缩（2026-09-23 用户选定）

- `config.json → cache_bars`：**`0` = 不限制，读入 `.day` 全部历史（现为默认）**；
  正数 = 只留最后 N 根。用户原话：「全部历史，现有数据什么时候开始就什么时候开始」。
- 规模：**1701 万行 / `bars.parquet` 360 MB / 内存 578.9 MB / 重建 18.8s**；
  5605 只平均 3035 根、最长 8591 根，最早 **1991-01-02**，交易日 **8786** 个。
- **「指定日期选不了 2022 年」的根因是两层**：① `cache_bars=260` 只装了最近一年
  （起点 2025-08-29）；② 前端写死 `/api/trade_dates?n=250` → 把 `input.min` 设成
  2025-09-12，日历直接灰掉。**两处都要改**。
  注意缓存里 `date.min()` 是 2024-03-25（3 只停牌股的残影），**不能拿它当可用起点**。
- **改 `cache_bars` 后必须重建缓存，且只比 mtime 是发现不了的** ——
  `cache_freshness()` 已加口径比对：`meta["bars_per_code"] != config["cache_bars"]` → 判过期
  （缺字段取 `-1`，保证老缓存也会重建）。
- **dtype 收缩（`tdx_reader.CACHE_DTYPES`）是硬约束，改前先扫极值算 ULP**：
  `code`→category（object 字符串单列就差 900MB）、`date`→int32、价格×4→float32、
  `amount`→float32、`pct`→float32，**`volume` 必须 uint32**（float32 在 41.7 亿股处
  ULP=256 股，前端「手」会偏 1~2 手，与通达信对不上；源本身就是 u4，无损）。
- **per-frame 共享同一个 `CategoricalDtype`**：先 concat 成 object 再转 category 会多占 900MB。
- `bars()` 会 `_coerce_bars_dtypes()` 兜旧缓存（老 parquet 的 `code` 是 object 字符串，
  直接读会让 `df["code"].cat.codes` 崩）。

### 「不落列、按需实时算」（用户明确要求）

`bar_index` / `bar_total` / `ma_vol` / `vol_ratio` **一律不存**，全部按需算：

| 原列 | 现在 | 省 |
|---|---|---|
| `bar_index` | 全项目无人用 → 直接不存 | — |
| `bar_total` | `_category_bar_counts(upto)`：5605 类别上一次 searchsorted，22 KB | 136 MB |
| `ma_vol`/`vol_ratio` | `_attach_volume_stats(idx, window)`：只对命中行求前 window 根之和（~11 万元素），float64 逐值精确 | 136 MB |

- `bar_total` 必须按**锚点日**取值，不是「整段历史长度」—— 否则回看 2022 年时，
  2022-05 才上市的票会被误判成「K 线充足」。
- 选股过滤也改到**类别层**：`split_code`/板块判定只对 5605 个类别算掩码，
  再 `cat_ok[cat_codes]` 展开成行级布尔（避免 1700 万行逐行 `.map(dict)`，那会又慢又费内存）。
  历史日期选股实测 **0.11s**。
- `_attach_volume_stats` 返回的小表把 `code` 还原成 `object` str（最多 5605×lookback 行），
  下游 `map`/`isin`/`itertuples` 就与以前完全一致，没有 categorical 边界情况。

回归脚本：`packaging/fullhistory_test.py`（规模/内存/dtype 明细、与 `.day` **逐只根数核对**、
2022 选股、均量口径**独立重算逐值比对**、新鲜度口径、旧缓存兼容）。

## 计划/追踪持久化（用户强调「不要丢失」）

- SQLite：`stock_picker/data/stock_picker.db`，`journal_mode=WAL` + `synchronous=FULL`
- 自动备份：启动时 + 写入后（间隔 > 6h），SQLite 官方 `backup()` 在线快照，
  存 `data/backups/`，保留最近 40 份；另有 `POST /api/storage/backup` 手动触发
- 恢复方式：停服务后把备份 `.db` 覆盖回 `stock_picker.db`
- **注意**：WAL 模式会产生 `-wal`/`-shm` 附属文件，
  拷贝数据库必须走 SQLite backup API，不能直接复制 `.db` 文件
- **测试脚本不得整表清空**：`packaging/smoke_test.py` 为验证 CRUD 会写入计划/追踪，
  但跑完必须**只删自己刚创建的那两条**（记下接口返回的 id 再 `DELETE /api/plans/{id}`、
  `/api/watchlist/{id}`）。库里长期有用户自己的计划（2026-09-22 实测有 3 条用户数据），
  「跑完清空 plans/watchlist」的写法会误删用户数据 —— 动库前先 `SELECT` 看明细。
- **`persist_test.py` 必须跑在独立 HOME**（`dist/_persist_test_home`，用 `env=ENV` 传
  `STOCK_PICKER_HOME`）。它以前 `HOME = EXE.parent` 且清理时遍历接口全部条目 DELETE，
  在库里有用户数据之后跑一次就会**全删光**（当年库是空的才没出事）。收尾要停实例 + 删 HOME。
- **不要在测试里写死期望值**：`A 股总数 = 5602`、`命中数 = 212` 都因为数据更新变成过期的假失败。
  只写不变量与交叉核对：与本地目录实时统计对齐、同参数两次结果一致、
  每行都满足条件（量比≥2 / 主板 / 非 ST）、命中数在合理区间。
- **跑打包版冒烟测试：起服务 + 测试 + `taskkill` 必须塞进同一次 Bash 调用**。
  前台 Bash 调用里 `nohup ... &` 起的 exe 会在该调用返回时被**按进程组回收**，
  下一个调用去测就是满屏红（`502` / `upstream connect failed` / `bootstrap 超时未就绪`），
  极容易被误判成代码坏了。打包用 `stockpicker-build` venv：
  `C:\Users\Ming\.workbuddy\binaries\python\envs\stockpicker-build\Scripts\python.exe packaging/build.py`
  （managed 3.13.12 那个没装 PyInstaller）。
