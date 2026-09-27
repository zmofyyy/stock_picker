# MEMORY.md — stock_picker 工作区长期记忆

> 先读 `stock_picker/README.md`（功能/口径/API/校验/打包）。本文件只记**会随时间变的事实**与**踩过的坑**。

## 0. 位置 · 运行 · 环境

- 工作区 `E:\sourcecode\stock_picker`（旧笔记的 `F:\source\stock_rsi`、`F:\source\stock_blocks\stock_watch` **已不存在**）。
- 主体 `stock_picker/`：FastAPI + pandas/numpy，原生 JS 单页 + 本地 ECharts，**无构建步骤**。端口 **8778**。
- 页签 6 个：选股 / 连板梯队 / 板块看盘 / 板块交集 / 计划 / 追踪。启动即后台预加载。
- **解释器必须 `C:\Python313\python.exe`**；managed 的 `binaries\python\3.13.12` 缺 `annotated_doc`，导入即 `ModuleNotFoundError`。打包用 `envs\stockpicker-build\Scripts\python.exe`。
- Bash 先补 PATH（PortableGit `usr/bin`）；PowerShell 工具**不回显 stdout**；**不能从 Bash 调 powershell**。

## 1. HOME 两套数据（别搞混）

可写目录由 `config.py::APP_DIR` 决定，**计划/追踪跟着 HOME 走**。

| 启动方式 | HOME | 计划/追踪 |
|---|---|---|
| `dist/stock_picker.exe` | `dist/` | **真实数据**：plans 3（福龙马/华微电子/XD神通科）、watchlist 1（华微电子） |
| `python -m stock_picker.app` | `stock_picker/` | 独立空库 |

源码态看不到 exe 里的计划**不是数据丢失**；要共用设 `STOCK_PICKER_HOME=dist`。`dist/data/backups/` 27 份（`华纺股份` 只在备份里，测试残留）。

## 2. 本机行情（数字会变，脚本里别写死）

- 通达信 **`D:\new_tdx`**：`sh/sz/bj` 共 **9755** 个 `.day`；**A 股正股 5608 只**（5602 是历史值）。
- **个股从 2001-01-02 起**（旧笔记的 1991-01-02 错）；只有指数 `sh999999` 到 1990-12-19。
- **`vipdoc\ds\lday` 另有 19785 个 `.day`**（外汇/期货/全球指数）→ 自己 glob `vipdoc/*/lday` **必须限定 sh/sz/bj**。
- 索引：名称 52163 / 行业 5584 只·128 二级 / 概念 269 个·46586 归属 / 流通股本 5922 / 交易日约 6236。
- 缓存 `bars.parquet` **344MB / 1620 万行 / 5608 只 / 重建 18.8s**（`cache_bars: 0` = 全历史）。

## 3. 通达信数据格式

- **`.day` 定长 32 字节** `<IIIIIfII`：date/OHLC（uint32，**分**）、amount（float32，元）、volume（uint32，**股**）、prev_close（**2015-12-16 起全市场失效，别用**）。
- pytdx 返回元/**手**（多 ÷100）、不支持 688 与北交所；`np.frombuffer` 向量化与 pytdx 逐字段一致且**快 117 倍**。
- **`92xxxx` 是北交所**，判定必须在「`9` 开头 = 沪市」**之前**，否则读不到文件。
- **`*.tnf` 名称表**：N×360+50 字节；名称在 `[81:89]`（GBK），代码 = 名称前 40 字节内最后一段 6 位数字。
- **行业**：`incon.dat` 分段（`#TDXNHY` T 码 / `#TDXRSHY` X 码 / `#SWHY` 申万**无归属文件**）；归属在 `tdxhy.cfg`（`市场|代码|T码|||X码`）。**主体用 X 码**（二级取前 5 字符）；板块看盘的行业改走 **T 码**（§11）。
- **板块**：`infoharbor_block.dat`（`#<前缀>_<名>,<成员数>,<指数代码>,…` + 成分行，前缀 `GN_`269/`FG_`161/`ZS_`117）与 `tdxzs.cfg`（`名|880xxx|类型码|?|0|显示名`，**2 行业/3 地区/4 概念/5 风格**）。
  **两文件命名口径不同（36 个差异）→ 交叉验证必须按指数代码**；按代码求交 269/269 零残差。`tdxbk.cfg` 无成分，别用。

## 4. 编码约定

- 涨红 `#ff453a` / 跌绿 `#30d158`（中国习惯）、货币 ¥；均量取**前 N 日**（不含当日），不引入未来函数。
- 一切「按每只股自己最后一根 K 线」滑动的逻辑，必须锚在**全市场交易日**上，否则停牌/退市股冒充当期信号。
- **选股默认只保留主板**（用户 2026-09-21 两次明确要求）；板块判定统一 `tdx_reader.board_of()`；白名单 `boards` 传空列表**报错**，不静默回退。实测扫主板 **3227** 只 → 命中 **154** → 0.179s。

## 5. 涨跌停与连板（`limits.py`）

- 涨停价 = `(prev_c * f + 50) // 100`（**「分」整数**），`f` = 主板 110 / 创业·科创 120 / 北交所 130 / ST 105；**上市首日剔除**；跌停另要求「最低 ≥ 跌停价」（排除除权假跌停）。
- **⚠️ 本机主板 ST 股带宽也是 10%**（144 只里 141~142 只出现过「最高 > 前收×1.05」）→ **`st_limit` 默认关闭**。「ST」前缀 ≠ 5% 带宽。
- 封板形态：一字板（最低 = 涨停价）/ T字板（开 ≥ 涨停价但盘中打开）/ 换手板。
- 连板全向量化（`STREAK_WINDOW = 60`）：`streak_all = np.where(boundary, 0, gp - np.maximum.accumulate(np.where(boundary, gp, -1)))`，0.05~0.16s。
- **`as_multi()` 必须挡住裸字符串**：`[b for b in "主板"]` → `['主','板']` → 静默空结果不报错。

## 6. 前端约定（macOS Vibrancy 深灰）

- 三档深灰 `#1c1c1e`→`#2c2c2e`→`#3a3a3c`；1px 描边只用 `rgba(255,255,255,.08/.12)`；毛玻璃 `blur(28~30px) saturate(180%)`；过渡 200ms 且**只过渡颜色**；**无渐变/光晕/pulse**。
- **页内互斥切换一律做 tab，不用 `<select>`**（用户 2026-09-26 要求）。范例 `.bp-tabs`：`--deep` 底 + 1px 描边 + 内 padding 3px；`.bp-tab.active` 用**半透明蓝底 `rgba(10,132,255,.14)` + 蓝描边**（不用满色块，免得跟侧栏 accent 抢注意力）；数量徽章 `.bp-tab b:empty{display:none}`。**说明文字放外层 `.bp-tabs-row`** —— 带边框的控件容器里只放可点分段项。
- **tab = 模式，不是数据列**：类别一变成 tab，表里那列（整列同值）就该删 —— 板块表因此 10 列→9 列、`min-width` 560→510。切 tab = 换整个数据集，必须同步复位联动区（`clearBoardPicks()`）。
- **选股表不设操作列**，点整行开抽屉；`#screenTable` `min-width` 1100px（保持 `scrollWidth <= clientWidth`）。
- **分布卡基数必须独立于筛选链**：`summary_base` 在过滤**之前**取快照（曾放之后 → 点一个行业后其余 chip 全消失）；回归 `summary_base_test.py`。
- 抽屉 `min(1440px, 95vw)`，图高 620px；**`#klineChart` 必须是 `.drawer-body` 直接子元素** —— 曾多一个 `</div>` 被解析成 `.drawer` 子元素，图高 620→440 而页面照样打开（**沉默失败**）。
- 布局：`body` 滚动 + `.sidebar` fixed + `.toolbar` sticky（不用「fixed 内部滚动」，否则截图截不全）。
- 连板页行业/概念筛选走**本地** `streakFiltered()`；排序件 `orderBy/paintSortHeaders/bindSortHead` 多页共用；非默认页首次切过去才加载。

## 7. 行情缓存口径与内存账

- `cache_bars`：**`0` = 全历史（默认）**；改完必须重建，且只比 mtime **发现不了** —— `cache_freshness()` 要比对 `meta["bars_per_code"] != config["cache_bars"]`。
- **dtype 收缩（`CACHE_DTYPES`）是硬约束**：`code`→category（object 单列差 900MB）、`date`→int32、价格×4/`amount`/`pct`→float32；**`volume` 必须 uint32**（float32 在 41.7 亿股处 ULP=256 股，前端「手」偏 1~2 手）。
- **不落列、按需实时算**：`bar_total` 用 `_category_bar_counts(upto)`（**必须按锚点日取值**，否则上市晚的票被误判 K 线充足）；`ma_vol`/`vol_ratio` 用 `_attach_volume_stats()`。共省约 272MB。
- 选股过滤在**类别层**做（避免 1620 万行逐行 `.map(dict)`）。回归 `packaging/fullhistory_test.py`。

## 8. 生命周期 · 预加载 · 缓存指纹

- **用 `lifespan` 上下文管理器**（2026-09-25 从已弃用的 `@app.on_event` 迁来）；必须定义在 `app = FastAPI(lifespan=lifespan)` **之前**；`_on_shutdown()` 放 `yield` 后的 `finally` 里，保证 Ctrl+C 也执行。
- 预加载 8 步：名称表 → 行业分类 → 概念板块 → **板块目录**（§11）→ 流通股本 → 行情缓存 → 内存行情 → 交易日索引；已最新时整轮 **0.3~0.8s**。每步记进 `steps`（`/api/bootstrap`）。
- **过期判定**：`bars.parquet` mtime vs `vipdoc/{sh,sz,bj}/lday` 最新 `.day` mtime（`os.scandir`+`DirEntry.stat()`，9755 文件约 30ms，**不 fopen 全部**）+ 口径比对。
- **重建必须拿 `_refresh_lock` 并在锁内重判 freshness**；写 `.tmp` 后 `os.replace`（原子）。
- `/api/bootstrap` 的 **`ready` = 不在跑（失败也算）**，否则挂掉的预加载会卡死前端；前端 `waitBootstrap()` 完成后才 `doScreen()`。
- **前端指纹三道防线**（README §5.1）：响应头（`/static/*` `no-cache`；`/` 与 `/api/*` `no-store`）+ URL 指纹（`/` 必须用 `HTMLResponse` 替换 `__ASSET_VER__`，**不能走 `FileResponse`**）+ `/api/version` 自检。
  **铁律**：遇「改了前端没生效」先怀疑浏览器 —— ① `curl` 接口 ② `curl` 静态资源与源码 `diff` ③ 干净会话读 DOM。**纯前端改动不需重启**（`/static/*` 每次读盘，实测旧进程直接吐出新 `app.js`）。

## 9. 持久化（用户强调「不要丢失」）

- SQLite：`WAL` + `synchronous=FULL` + `busy_timeout=30000`；启动时 + 写入后（间隔 > 6h）用官方 `backup()` 在线快照 → `data/backups/`（留 40 份）。恢复 = 停服后覆盖。**`-wal`/`-shm` 不能用拷贝代替 backup API**。
- **测试脚本不得整表清空**；`persist_test.py` 必须跑**独立 HOME**（曾在 `dist/` 上跑，清理时遍历接口全部条目 DELETE，库里有用户数据就**全删光**）。
- **测试里别写死期望值**（`A 股=5602`、`命中=212` 都成了过期假失败）；只写不变量与交叉核对。
- **跑打包版冒烟：起服务 + 测试 + `taskkill` 必须塞进同一次 Bash 调用**。

## 10. 跨调用存活 · 浏览器自动化

- 起独立进程：**PowerShell `Start-Process` 在本机不可用**（报 `已添加项。字典中的关键字:"Path"`）→ Python `subprocess.Popen(..., creationflags=DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP)`，`env` 传 `STOCK_PICKER_HOME`，轮询 `ready`；收尾 `taskkill //F //PID <pid>`。
- **`agent-browser` 在本机基本不可用**（两次 `open` 各卡死 7 分钟+，也没 `resize`）→ 用 **headless Chrome + CDP**（冷启动到出图约 40s）。**Node 22 自带全局 `WebSocket`**，写零依赖驱动：`GET /json/list` 取 `webSocketDebuggerUrl` → `Page.enable`/`Runtime.enable`/`Emulation.setDeviceMetricsOverride`/`Page.navigate`/`Runtime.evaluate`（轮询 DOM）/`Page.captureScreenshot`（`captureBeyondViewport:true`）。**脚本 `%TEMP%\bp_shot.js` 可复用**（含 tab 切换 + 联动验证）。纯 API 验证用 `curl`。
- **直接指向正在跑的 8778 截图**即可；**别用 `--port` 改端口**（会写回 `config.json`）。

## 11. 板块看盘（`boards.py` + `service.board_*`）

- 结构：顶部**类别 tab** → 左上板块列表 **9 列** / 右上成分股列表 13 列 → 下排两张三栏图（**K 线 + 成交量 + MACD**）。
- **主视图两段 tab**：**概念板块（268 有成分）/ 行业板块（132）** —— 地区无成分文件、风格是因子指标，都不适合题材看盘。API **没下架**：`category=地区|风格` 仍可用，`/api/board_catalog` 仍返回四类 + `primary` 标记；`categories()` 还有 `label`，**输出顺序由 `PRIMARY_CATEGORIES` 决定**（不是类型码顺序）。tab 数字必须用 **`n_members`** 而非 `n`，否则 269/145 与实际行数 268/132 对不上。
- API：`/api/board_catalog`、`/api/board_panel`、`/api/board_members`（参数名 **`board`** 不是 `code`）、`/api/board_kline/{code}`。
- **604 板块**（概念 269/风格 158/行业 145/地区 32）/ 552 有成分 / 5964 只成分股；缓存 `data/boards.json` 约 1MB、加载 16ms。指数 `880xxx` 本地 **604/604 齐全**。
- **两个坑**：`tdxzs.cfg` 第 6 列**行业放 T 码、地区放序号**，`_CODE_LIKE = ^(?:[Tt]\d+|\d+)$` 命中就回退第 1 列；**概念/风格那 53 个「显示名 ≠ 简称」是真全称，必须保留**。改动要 +`PARSE_VERSION`。
- **行业走 T 码，不走主体的 X 码**（`tdxzs.cfg` 的 145 个行业名只与 `#TDXNHY` 对得上：132/145；X 码只对上 47）。T 码是层级码（3/5/7 字符），**股票归属都是三级码** → **二级板块必须按前缀聚合**（`bisect`）才有成分。实测 132 = 二级 56 + 三级 76。
  **⚠️ 一级 13 个（`880981`~`880993`）在 `tdxzs.cfg` 里叫「TDX 能源」这类别名 → 对不上名称表，目前无成分**（第 6 列确实是 `T01`~`T13`，可按 T 码救回）。
- **指数行情不在主缓存**（`is_a_share` 排除指数）→ `_index_bars()` **seek 文件尾部**只读需要的根数（604 个齐扫 ~20ms，整读 0.9s）。**缓存键必须 `(mtime, 已读根数, df)`** —— 只按 mtime 会把 22 根那份交给要 200 根的 K 线图。顺手补 `pct = close.pct_change()`。
- 聚合：`(板块,股票)` 展平成对 → merge 锚点日快照 → **一次 `groupby`**。`summary.boards_*` 在 `limit` 截断**之前**统计；`summary.limit_up_stocks` 是**全市场去重**口径，与 `/api/streaks` 的 `metrics.limit_up` 相等。
- 前端：`.bp-split` 必须 `minmax(0,·fr)`（否则被表撑破）；`.bp-pane` 本身是 grid 子项、**两张卡天然等高**（实测 553.06/553.06）；排序缺值永远沉底（`_sorted_rows`）。
- 回归 `packaging/board_test.py`（**117 项 / 9 节**）：`STOCK_PICKER_HOME=<独立HOME> C:\Python313\python.exe packaging/board_test.py`。

## 12. 验证方法论

- **别用截图当唯一证据**：同一趟浏览器脚本里把 `getBoundingClientRect()` / `innerText` / `scrollWidth` / `getComputedStyle()` 一起取回 —— 数值可靠，像素会看错。**主题配色同理**（曾误判页面渲染成浅色，实测 `body` 为 `rgb(28,28,30)`、反色像素数全为 0）。**对比页面与接口前先对齐入参**（曾因页面默认 `with_members_only=1` 误判口径不一致）。细节案例见当日日志。
- 测试里**别重写路径规则**（`incon.dat` 在 TDX **根目录**，不在 `hq_cache`）→ 用模块自己的 `BI._sources()`。
- 别写恒真的「占位断言」：判断「前缀聚合生效」要查**子集关系**且**核对对数 > 0**（否则空转通过）。
- **已知小瑕疵（非 bug，别去查）**：`service` 上没有 `ready` 属性，测试开头的 `getattr(service, "ready", False)` **恒为 False**、总会多跑一次很便宜的 `refresh()`。
