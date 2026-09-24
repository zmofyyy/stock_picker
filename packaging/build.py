"""把 stock_picker 打包成 Windows exe。

用法：:

    python packaging/build.py              # 单文件 exe（默认，dist/stock_picker.exe）
    python packaging/build.py --onedir     # 目录版（启动更快，dist/stock_picker/）
    python packaging/build.py --both       # 两种都出
    python packaging/build.py --console    # 保留控制台（默认就是控制的）

前置：``pip install pyinstaller``（>=6.0）。

产物会在 `dist/` 下，并在产物旁边放一份 `使用说明.txt`。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
PACKAGE_DIR = PROJECT_ROOT / "stock_picker"
ENTRY = HERE / "entry.py"
ICON = HERE / "stock_picker.ico"
DIST_DIR = PROJECT_ROOT / "dist"
WORK_DIR = PROJECT_ROOT / "build"
APP_NAME = "stock_picker"

# uvicorn 用字符串在字典里查实现类，PyInstaller 静态分析看不到，必须显式声明。
# 本机没装 httptools / websockets / uvloop，会自动退回 h11 + asyncio，
# 但 auto 模块仍要打包，否则 uvicorn.config 查表时会 KeyError。
HIDDEN_IMPORTS = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # pandas 的 parquet 引擎是运行时按名字 import 的
    "pyarrow",
    "pyarrow.parquet",
    "pyarrow._parquet",
    "pyarrow.lib",
    "pyarrow._compute",
    "pyarrow.vendored",
    # pytdx 的 reader 是函数内 import，且 shares.py 里是从子模块直接取类
    "pytdx",
    "pytdx.reader",
    "pytdx.reader.gbbq_reader",
    "pytdx.reader.day_reader",
    "pytdx.reader.tdx_reader_base",
    "pytdx.parser",
    "pytdx.parser.gbbq_parser",
    # 引擎层
    "anyio._backends._asyncio",
    "email.mime.text",
]

# 明显用不到的大件，剔掉能省 30~60MB。
# 原则：只剔「本程序任何代码路径都不会 import」的东西，
# pandas/numpy 的核心与 pyarrow.parquet 一律保留，宁可大一点也不要在运行时报 ImportError。
EXCLUDES = [
    "matplotlib", "scipy", "tkinter", "PIL", "IPython", "jupyter", "notebook",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "pytest", "sphinx", "docutils",
    "pandas.tests", "numpy.tests",
    "sqlalchemy", "boto3", "botocore", "s3fs",
    "pyarrow.flight", "pyarrow.gandiva", "pyarrow.cuda",
    "pyarrow._substrait", "pyarrow._azurefs", "pyarrow._s3fs", "pyarrow._gcsfs",
    "pyarrow._hdfs", "pyarrow.dataset", "pyarrow.orc", "pyarrow.parquet.encryption",
]


def _pyinstaller_cmd(onefile: bool, keep_console: bool) -> list[str]:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        str(ENTRY),
        "--name", APP_NAME,
        "--noconfirm",
        "--clean",
        "--noconsole" if not keep_console else "--console",
        "--onefile" if onefile else "--onedir",
        "--distpath", str(DIST_DIR),
        "--workpath", str(WORK_DIR),
        "--specpath", str(WORK_DIR),
        # 让 `import stock_picker` 能被解析到
        "--paths", str(PROJECT_ROOT),
        # 内置静态资源必须一起打进去（RESOURCE_DIR/static）
        "--add-data", f"{PACKAGE_DIR / 'static'}{';' if sys.platform == 'win32' else ':'}stock_picker/static",
    ]
    if ICON.is_file():
        cmd += ["--icon", str(ICON)]
    for m in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", m]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    return cmd


README = """stock_picker —— 放量选股 / 连板梯队 / 板块交集 / 计划 / 追踪
==============================================================

一、启动
  双击 stock_picker.exe。稍等几秒（单文件版要先解包），
  浏览器会自动打开 http://127.0.0.1:8778/

  打开就能用：程序已在后台把本地数据（名称 / 行业 / 概念 / 股本 /
  行情缓存）全部载入，页面打开后还会自动跑一次选股 ——
  不用先点「刷新数据」再点「开始选股」。
  若本地通达信行情比缓存新、或缓存的历史深度与 cache_bars 配置对不上，
  会在页面顶部显示进度条并自动重建（全历史约 20 秒）。

二、数据放在哪
  数据目录 = exe 所在目录（首次启动会自动创建）：
    config.json          运行配置，可直接用记事本改
    data/stock_names.json
    data/industries.json
    data/concepts.json
    data/float_shares.json
    data/cache/bars.parquet
    data/stock_picker.db      计划与追踪（SQLite）
    data/backups/             自动备份

  **整个目录可以直接拷走**，拷到别的机器上数据照旧。
  换地方放数据也行，设环境变量 STOCK_PICKER_HOME 指向目标目录即可。
  注意：计划 / 追踪是跟着这个目录走的 —— 换目录或另跑一份源码版，
  看到的会是另一套数据。

三、必须配置的一项
  config.json 里的 "tdx_dir" 要指向你自己的通达信安装目录
  （形如 D:\\new_tdx，里面应有 vipdoc\\sh\\lday、vipdoc\\sz\\lday）。
  也可以在启动时用参数覆盖：
    stock_picker.exe --tdx-dir "D:\\new_tdx"

四、常用参数
  --port 8779          换端口（默认 8778）
  --no-browser         不自动打开浏览器
  --tdx-dir <路径>     指定通达信目录
  --reader fast|pytdx  读取方式，默认 fast

五、注意
  - 端口已被本程序占用时，再次双击只会打开页面，不会起第二个实例。
  - 程序读的是本地通达信日线（不复权），请先在通达信客户端完成盘后下载。
  - 选股结果仅为数据统计，不构成投资建议。
  - 首次启动要建缓存（全历史约 20 秒），之后是秒级；本地行情有更新时会自动重建。
  - 不想自动加载？把 config.json 的 bootstrap.preload 改成 false。

六、历史深度（重要）
  config.json 里的 "cache_bars" 决定每只股票缓存多少根 K 线：
    0      = 不限制，读入本地 .day 的全部历史（默认，可回溯到 1991 年）
    正整数 = 只保留最后 N 根（例如 260 约等于最近一年）

  「指定日期」控件的可选范围就等于这个区间 —— 设成 0 才能选 2022 年这类
  更早的年份。全市场全历史是 1700 万行，缓存文件约 360 MB、程序占用内存
  约 1.0~1.4 GB，首次重建约 20 秒；改成较小的 cache_bars 会等比例变小。

  注意：改了 cache_bars 后需要重建缓存，程序会**自动**识别并重建
  （只比对文件时间戳是发现不了这个变化的）。

七、连板梯队
  侧栏「连板梯队」页：把指定交易日的涨停个股按连板高度分层，
  看市场高度与主线。

  - 连板数 = 从所选交易日往回连续涨停的天数（首板 = 1）。
  - 涨停按涨跌幅限制精确判定：主板 10% / 创业板·科创板 20% /
    北交所 30%，上市首日剔除；涨停价按「分」四舍五入。
  - 附带：指标概览（首板 / 2 连板 / 3 连板以上 / 最高连板 / 晋级率）、
    连板高度分布图、梯队视图（标出封板形态：一字板 / T字板 / 换手板）、
    涨停股的二级行业与概念分布（点行即筛选）、明细表（点行看 K 线）。
  - 日期控件可选本地数据的全部交易日（1991 年起）。
  - 选到非交易日会明确报错并给出可选范围，不会悄悄换成别的日期。

  「ST 按 5% 判定」开关**默认不勾**。实测本机行情里主板 ST 股的日内
  涨跌幅限制也是 10%（144 只里有 141 只出现过单日最高价超过前收 5% 的
  情况），按 5% 判会把涨 5%~10% 的普通交易日错算成涨停。若你的行情源
  确实带 5% 带宽，再把这个开关打开。

八、板块交集
  侧栏「板块交集」页：求「二级行业 ∩ 概念」的交集 —— 哪些票同时属于
  选中的行业、又属于选中的概念。与选股 / 连板不同，这里只做板块归属的
  集合运算，不看成交量。

  - 左栏选二级行业（多选 = 并集，按一级行业分组，标签上的数字是该行业
    的成员数）；右栏选概念（多选可选「任一」或「全部命中」）。
  - 点标签即选中/取消，改完自动重算（约 0.1 秒），不用反复点按钮。
  - 上方画出 A / A∩B / B 三个数（文氏图 + 指标卡），看得出交集是把哪一侧
    收窄了。
  - 两侧都不选不会算，会提示你先选一个；行业名或概念名写错也会明确报错。
  - 交集为 0 时，页面会给出「A 里最常出现的概念」与「B 里最常出现的行业」
    两组标签，点一下就并入条件并立刻重算。
  - 明细表可排序，点行看 K 线，抽屉里能直接「加入计划 / 加入追踪」。

  另外：板块成分取自本地通达信板块文件，只有一份 —— 所以查历史日期时是
  「用今天的板块成分看历史行情」，不是历史快照，页面会明确提示。

  提示：这一页也能当「某行业里哪只票概念最多」来用 —— 只选行业，
  然后把「概念数」列按降序排。

八、换了新版程序后
  重新下载/替换 exe 后，**已经打开的页面要刷新一次**（F5，
  保险起见 Ctrl+Shift+R）—— 页面里的脚本是加载时那版，不刷新就还是旧功能。

  程序做了三层保护，避免你对着一个旧页面排查「改了怎么没生效」：
    1. 页面 HTML 与接口响应都不允许浏览器缓存；
    2. 页面引用的 app.js / style.css 带内容指纹（?v=xxxx），
       程序一换，地址就变，浏览器必然重新下载；
    3. 页面每 20 秒（以及每次切回该标签页时）比对一次版本，
       发现自己过期就在顶部弹一条橙色提示，点「刷新页面」即可。
"""


def _collect_env() -> dict:
    env = dict(os.environ)
    # 避免把源码目录带进打包结果（否则 PyInstaller 会把整个工作区当搜索路径）
    env.pop("PYTHONPATH", None)
    return env


def _run(onefile: bool, keep_console: bool) -> None:
    if not ICON.is_file():
        print("[icon] 生成图标…")
        subprocess.run([sys.executable, str(HERE / "make_icon.py")], check=True)

    cmd = _pyinstaller_cmd(onefile, keep_console)
    kind = "单文件" if onefile else "目录版"
    print(f"\n=== 打包（{kind}）===")
    print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=_collect_env())
    dt = time.perf_counter() - t0
    if proc.returncode != 0:
        print(f"打包失败，退出码 {proc.returncode}")
        raise SystemExit(proc.returncode)
    print(f"打包完成，用时 {dt:.1f}s")

    target = DIST_DIR / (f"{APP_NAME}.exe" if onefile else APP_NAME)
    if not target.exists():
        print(f"预期产物不存在：{target}")
        raise SystemExit(1)
    if target.is_file():
        print(f"产物：{target}  {target.stat().st_size / 1e6:.1f} MB")
    else:
        size = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
        print(f"产物：{target}\\{APP_NAME}.exe  （目录合计 {size / 1e6:.1f} MB）")

    # 配置模板：目标不存在才放，避免覆盖用户改过的配置
    cfg_src = PACKAGE_DIR / "config.json"
    cfg_dst = DIST_DIR / "config.json"
    if cfg_src.is_file() and not cfg_dst.exists():
        shutil.copy2(cfg_src, cfg_dst)
        print(f"已放置配置模板：{cfg_dst}")

    note = DIST_DIR / "使用说明.txt"
    note.write_text(README, encoding="utf-8")
    print(f"已写出说明：{note}")


def main() -> None:
    ap = argparse.ArgumentParser("build")
    ap.add_argument("--onedir", action="store_true", help="只出目录版")
    ap.add_argument("--onefile", action="store_true", help="只出单文件版（默认）")
    ap.add_argument("--both", action="store_true", help="两种都出")
    ap.add_argument("--windowed", action="store_true",
                    help="隐藏控制台窗口（默认保留控制台，便于看日志）")
    args = ap.parse_args()
    keep_console = not args.windowed

    try:
        import PyInstaller  # noqa: F401
    except ModuleNotFoundError:
        print("未安装 PyInstaller。请先执行：\n  python -m pip install pyinstaller")
        raise SystemExit(1)

    jobs: list[bool] = []
    if args.both:
        jobs = [True, False]
    elif args.onedir:
        jobs = [False]
    else:
        jobs = [True]

    for onefile in jobs:
        _run(onefile, keep_console)


if __name__ == "__main__":
    main()
