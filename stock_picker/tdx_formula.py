"""通达信量化公式引擎（``tqcenter``）封装。

通达信 64 位客户端自带一套 Python 量化接口：

- 脚本：``<tdx_dir>/PYPlugins/sys/tqcenter.py``
- 原生库：``<tdx_dir>/PYPlugins/TPythClient.dll``

它通过 DLL 连到**正在运行**的 ``TdxW.exe`` 进程。实测**外部 Python 进程**
（3.13 / 3.14 均可）可以直接 ``initialize`` 并调用，不需要把脚本塞进客户端
的「策略管理器」里跑 —— 这一点是本模块成立的前提。

能力面（本模块用到的部分）：

============================  ==============================================
``formula_get_all(type)``     列公式：0 技术指标 / 1 条件选股 / 2 专家系统
``formula_get_info(type, c)`` 取参数元信息：``ParaName`` / ``Min`` / ``Max`` /
                              ``Default``，以及输出线名
``formula_process_mul_xg()``  全市场批量选股（实测 5580 只约 5.5 秒）
``get_stock_list(market)``    股票池（``market='5'`` = 全部 A 股，含北交所）
============================  ==============================================

.. warning::
   **参数不能留空。** 实测 ``MA买入`` 传空 ``formula_arg`` → 全市场 0 命中，
   传 ``"5,10"`` → 12 命中。所以一律用公式自己的 ``Default`` 预填，
   ``normalize_args()`` 负责这件事。

.. warning::
   返回值的形状取决于 ``return_date``：
   ``False`` → ``{code: {'UP3': ['0', '1']}}``；
   ``True``  → ``{code: {'UP3': [{'Date': '20261013', 'Value': '0'}]}}``。
   本模块统一用 ``True``，便于同时拿到信号日期。

.. warning::
   **批量接口的 ``start_time`` / ``end_time`` 是失效的**（此 SDK 版本）。
   实测传了也照样返回「最新一根」；``count=0`` 或 ``count=-1`` 走区间分支时
   直接触发「批量执行异常」并返回空。因此**不能**靠 SDK 让公式算历史某天。

   正确做法是「一次扫描取窗口内多点，本地按日期挑值」：

   - ``count`` **既是喂给公式的 K 线根数，也是返回点数**（实测
     ``min(return_count, count)`` 个点，前几根会是 ``null`` 预热值）；
   - 本模块让 ``count == return_count == window``，于是一次调用就拿到
     末尾 ``window`` 个交易日的输出；
   - ``run_xg(signal_date=...)`` 只是在这批点里**本地挑出目标日**那一个去判命中。

   全市场 ``window=30`` 实测约 5.4s（与 ``window=5`` 基本持平）；客户端本地
   日线够深（``window=1000`` 能取到 2022-08）。
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# 公式类型：SDK 里的 formula_type
FORMULA_TYPES: Dict[str, int] = {
    "zb": 0,      # 技术指标（算数值，不判条件）
    "xg": 1,      # 条件选股（本页面主用）
    "exp": 2,     # 专家系统（含 ENTERLONG / EXITLONG）
}
TYPE_LABELS: Dict[int, str] = {0: "技术指标", 1: "条件选股", 2: "专家系统"}

# 命中值：SDK 返回字符串 '1' / '1.0'（也见过数值 1.0）
_TRUE_TOKENS = {"1", "1.0", "True", "true"}


class TdxFormulaError(RuntimeError):
    """公式引擎不可用（客户端没开 / SDK 缺失 / 版本不匹配）。"""


def _fmt_num(raw: Any) -> str:
    """把 '3.0000' 这类参数值写成 '3'；非整数保留原样。"""
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return str(raw).strip()
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return f"{f:g}"


def _norm_date(v: Any) -> str:
    """``2026-10-09`` / ``2026/10/09`` / ``20261009`` → ``'20261009'``。

    空值或长度不对的一律返回 ``''``（＝不指定，用窗口最后一根）。
    """
    s = str(v or "").strip().replace("-", "").replace("/", "")
    return s if len(s) == 8 and s.isdigit() else ""


def _ymd_to_iso(v: Any) -> str:
    """``'20261009'`` → ``'2026-10-09'``（已带横杠的原样返回）。"""
    s = str(v or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def normalize_args(info: Optional[Dict[str, Any]], args: Any) -> str:
    """把参数规整成 SDK 要的 ``"a,b,c"``；缺位用公式自带的 ``Default`` 补。

    ``args`` 可以是 ``"5,10"`` / ``[5, 10]`` / ``None``；空位一律回落到默认值，
    因为**空参数会让公式直接失效**（见模块文档）。
    """
    paras = (info or {}).get("Para") or []
    if not paras:
        # 无参公式：显式传空串（SDK 内部会转成 [None]，实测可用）
        return ""
    if args is None:
        given: List[str] = []
    elif isinstance(args, str):
        given = [s.strip() for s in args.replace("，", ",").split(",")]
    else:
        given = [str(a).strip() for a in args]

    out: List[str] = []
    for i, p in enumerate(paras):
        raw = given[i] if i < len(given) and given[i] != "" else p.get("Default", "")
        token = _fmt_num(raw)
        if token == "" or token.lower() in ("nan", "none"):
            token = _fmt_num(p.get("Default", 1))
        # 夹到 SDK 给的 [Min, Max] 区间内，避免客户端返回错误
        try:
            v = float(token)
            lo, hi = float(p.get("Min", v)), float(p.get("Max", v))
            if v < lo or v > hi:
                v = min(max(v, lo), hi)
                token = _fmt_num(v)
        except (TypeError, ValueError):
            pass
        out.append(token)
    return ",".join(out)


def _line_points(line: Sequence[Any]) -> List[Dict[str, str]]:
    """把一条输出线规整成 ``[{'date','value'}, ...]``（保持 SDK 给的顺序）。

    ``Value`` 为 ``null``（公式预热期）时写成空串，交给 :func:`is_hit` 判假。
    """
    out: List[Dict[str, str]] = []
    for item in line or ():
        if isinstance(item, dict):
            val, date = item.get("Value"), item.get("Date")
        else:
            val, date = item, None
        out.append(
            {
                "value": "" if val is None else str(val),
                "date": str(date) if date else "",
            }
        )
    return out


def _line_hits(line: Sequence[Any]) -> Optional[Dict[str, str]]:
    """判断一条输出线的**最后一点**是否命中，返回 ``{'date','value'}``。"""
    pts = _line_points(line)
    return dict(pts[-1]) if pts else None


def _point_on(points: Sequence[Dict[str, str]], date: str) -> Optional[Dict[str, str]]:
    """取**恰好落在 ``date`` 那一天**的点；没有就返回 ``None``。

    刻意不做「吸附到前一根」：长期停牌股的 K 线停在更早的日子，吸附会把它
    几十天前的旧信号冒充成目标日的信号。目标日是否落在窗口里，由调用方
    用「窗口共同交易日」统一解析，而不是每只股票各吸各的。
    """
    if not points or not date:
        return None
    for p in reversed(points):
        if p["date"] == date:
            return dict(p)
        if p["date"] and p["date"] < date:
            break          # 点是升序的，越过目标日就不可能再有匹配
    return None


def is_hit(value: Any) -> bool:
    """命中判定：SDK 返回字符串 '1'，也兼容数值 1。"""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value) == 1.0
    return str(value).strip() in _TRUE_TOKENS


class TdxFormula:
    """通达信公式引擎的进程内单例包装（线程安全）。"""

    def __init__(
        self,
        tdx_dir: str,
        sdk_dir: str = "",
        dll_path: str = "",
        window: int = 500,
        dividend_type: int = 1,
        max_window: int = 500,
    ) -> None:
        self.tdx_dir = Path(tdx_dir)
        self.sdk_dir = Path(sdk_dir) if sdk_dir else self.tdx_dir / "PYPlugins" / "sys"
        self.dll_path = Path(dll_path) if dll_path else self.tdx_dir / "PYPlugins" / "TPythClient.dll"
        self.max_window = max(1, int(max_window))
        # 窗口 = 喂给公式的 K 线根数 = 每股返回的输出点数（见模块文档）
        self.window = max(1, min(int(window), self.max_window))
        self.dividend_type = int(dividend_type)

        self._lock = threading.RLock()
        self._tq: Any = None
        self._ready = False
        self._last_error = ""
        self._formula_cache: Dict[int, List[Dict[str, Any]]] = {}
        self._info_cache: Dict[str, Dict[str, Any]] = {}
        self._stock_cache: Optional[List[str]] = None

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    @property
    def sdk_file(self) -> Path:
        return self.sdk_dir / "tqcenter.py"

    def sdk_present(self) -> bool:
        return self.sdk_file.is_file() and self.dll_path.is_file()

    def _import_sdk(self) -> Any:
        """把 SDK 目录加进 ``sys.path`` 并 import ``tqcenter.tq``。"""
        if self._tq is not None:
            return self._tq
        if not self.sdk_present():
            missing = []
            if not self.sdk_file.is_file():
                missing.append(str(self.sdk_file))
            if not self.dll_path.is_file():
                missing.append(str(self.dll_path))
            raise TdxFormulaError(
                "未找到通达信量化接口文件：" + "；".join(missing)
                + "。请确认 ``tdx_dir`` 指向 64 位客户端安装目录，"
                "且该目录下存在 PYPlugins/sys/tqcenter.py 与 PYPlugins/TPythClient.dll"
            )
        sdk_str = str(self.sdk_dir)
        if sdk_str not in sys.path:
            sys.path.insert(0, sdk_str)
        mod = importlib.import_module("tqcenter")
        self._tq = mod.tq
        return self._tq

    def _connect(self) -> Any:
        """确保已 ``initialize``。失败抛 :class:`TdxFormulaError`。"""
        tq = self._import_sdk()
        if getattr(tq, "_initialized", False):
            self._ready = True
            return tq
        # 脚本标识：用 SDK 自身路径，稳定且一定存在（打包后 __file__ 会漂）
        try:
            tq.initialize(str(self.sdk_file), dll_path=str(self.dll_path))
        except Exception as exc:  # RuntimeError("启动TPythClient失败") 等
            self._ready = False
            self._last_error = str(exc)
            raise TdxFormulaError(
                "连接通达信客户端失败：" + str(exc)
                + "。请确认对应版本的客户端（TdxW.exe）正在运行"
            ) from exc
        self._ready = True
        self._last_error = ""
        return tq

    def available(self) -> Dict[str, Any]:
        """探测可用性（不抛异常），供前端展示状态。"""
        info: Dict[str, Any] = {
            "sdk_dir": str(self.sdk_dir),
            "dll_path": str(self.dll_path),
            "sdk_present": self.sdk_present(),
            "connected": False,
            "ok": False,
            "reason": "",
        }
        if not info["sdk_present"]:
            info["reason"] = "未找到 tqcenter.py / TPythClient.dll"
            return info
        try:
            tq = self._connect()
            info["connected"] = bool(getattr(tq, "_initialized", False))
            info["run_id"] = int(getattr(tq, "run_id", -1))
            info["ok"] = info["connected"]
        except Exception as exc:
            info["reason"] = str(exc)
            return info
        return info

    def close(self) -> None:
        with self._lock:
            tq = self._tq
            if tq is not None:
                try:
                    tq.close()
                except Exception:
                    pass
            self._ready = False

    # ------------------------------------------------------------------
    # 公式目录
    # ------------------------------------------------------------------
    def formulas(self, ftype: int = 1, refresh: bool = False) -> List[Dict[str, Any]]:
        """列出某类公式（``acCode`` / ``acName`` / ``isSys``）。"""
        ftype = int(ftype)
        with self._lock:
            if not refresh and ftype in self._formula_cache:
                return self._formula_cache[ftype]
            tq = self._connect()
            raw = tq.formula_get_all(formula_type=ftype) or []
            out: List[Dict[str, Any]] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("acCode") or "").strip()
                if not code:
                    continue
                out.append(
                    {
                        "code": code,
                        "name": str(item.get("acName") or code).strip(),
                        "is_sys": int(item.get("isSys") or 0) == 1,
                    }
                )
            out.sort(key=lambda x: (not x["is_sys"], x["code"]))
            self._formula_cache[ftype] = out
            return out

    def formula_info(self, code: str, ftype: int = 1) -> Dict[str, Any]:
        """取公式的参数元信息（含 ``Para`` 与输出线名）。"""
        key = f"{int(ftype)}|{code}"
        with self._lock:
            if key in self._info_cache:
                return self._info_cache[key]
            tq = self._connect()
            raw = tq.formula_get_info(formula_type=int(ftype), formula_code=code) or {}
            info = dict(raw) if isinstance(raw, dict) else {}
            self._info_cache[key] = info
            return info

    def describe(self, code: str, ftype: int = 1) -> Dict[str, Any]:
        """公式完整描述：名称 + 参数定义（带默认值）+ 输出线。"""
        info = self.formula_info(code, ftype)
        paras = []
        for p in info.get("Para") or []:
            if not isinstance(p, dict):
                continue
            paras.append(
                {
                    "name": str(p.get("ParaName") or ""),
                    "min": p.get("Min"),
                    "max": p.get("Max"),
                    "default": _fmt_num(p.get("Default", "")),
                }
            )
        lines = [
            str(l.get("LineName") or "")
            for l in (info.get("Line") or [])
            if isinstance(l, dict)
        ]
        return {
            "code": code,
            "name": str(info.get("acName") or code),
            "type": int(ftype),
            "type_label": TYPE_LABELS.get(int(ftype), ""),
            "is_sys": int(info.get("isSys") or 0) == 1,
            "para_num": int(info.get("ParaNum") or len(paras)),
            "paras": paras,
            "lines": lines,
            "default_args": normalize_args(info, None),
        }

    # ------------------------------------------------------------------
    # 股票池
    # ------------------------------------------------------------------
    def stock_list(self, refresh: bool = False) -> List[str]:
        """全部 A 股代码（``000001.SZ`` 形式，含北交所）。"""
        with self._lock:
            if not refresh and self._stock_cache:
                return list(self._stock_cache)
            tq = self._connect()
            codes = [str(c) for c in (tq.get_stock_list(market="5") or [])]
            self._stock_cache = codes
            return list(codes)

    # ------------------------------------------------------------------
    # 批量选股
    # ------------------------------------------------------------------
    def run_xg(
        self,
        code: str,
        args: Any = None,
        stock_list: Optional[Sequence[str]] = None,
        window: Optional[int] = None,
        dividend_type: Optional[int] = None,
        signal_date: Any = "",
    ) -> Dict[str, Any]:
        """执行条件选股公式，返回命中明细。

        :param window: 回看窗口（交易日）。SDK 里 ``count`` 与 ``return_count``
            取同一个值 —— 于是**一次调用**就拿到末尾 ``window`` 个交易日的输出，
            ``signal_date`` 只是在这批点里本地挑一个去判命中。
        :param signal_date: 目标信号日（``20261009`` / ``2026-10-09``）。
            留空 = 窗口最后一根。窗口是**共同交易日**（一半以上股票都有那根）
            构成的，非交易日会整体吸附到不晚于它的最近共同交易日，并用
            ``date_adjusted`` 说明；早于整个窗口则整体失败并提示调大窗口。
            命中判定锁死在那一天：当日停牌 / 上市晚于该日的股票**不算命中**。

        :returns: ``{'ok', 'hits': [{'code','line','value','date'}], 'signals', 'errors',
                    'window', 'date', 'window_start', 'window_end', 'dates', 'date_hits',
                    'date_adjusted', 'elapsed', 'total', 'scanned', 'args', 'reason'}``
        """
        target = _norm_date(signal_date)
        win = int(window if window is not None else self.window)
        win = max(1, min(win, self.max_window))
        with self._lock:
            t0 = time.perf_counter()
            info = self.formula_info(code, FORMULA_TYPES["xg"])
            arg_str = normalize_args(info, args)
            tq = self._connect()
            stocks = list(stock_list) if stock_list else self.stock_list()
            try:
                raw = tq.formula_process_mul_xg(
                    formula_name=code,
                    formula_arg=arg_str,
                    # count 与 return_count 同取 win：喂 win 根、返回 win 个点
                    return_count=win,
                    return_date=True,
                    stock_list=stocks,
                    stock_period="1d",
                    count=win,
                    dividend_type=int(
                        self.dividend_type if dividend_type is None else dividend_type
                    ),
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "reason": f"公式执行失败：{exc}",
                    "hits": [],
                    "signals": {},
                    "args": arg_str,
                    "window": win,
                    "elapsed": round(time.perf_counter() - t0, 3),
                }

        if not raw:
            return {
                "ok": False,
                "reason": "公式无返回：公式代码可能不存在，或客户端尚未下载盘后数据",
                "hits": [],
                "signals": {},
                "args": arg_str,
                "window": win,
                "elapsed": round(time.perf_counter() - t0, 3),
            }

        hits: List[Dict[str, Any]] = []
        signals: Dict[str, Dict[str, Any]] = {}
        errors: Dict[str, str] = {}
        date_hits: Dict[str, int] = {}
        scanned = 0
        occ: Dict[str, int] = {}

        # --- 第一遍：摸清「窗口的共同交易日」 -------------------------
        # 不能直接用所有日期的**并集**：长期停牌股的 K 线停在更早的日子，
        # 会把窗口起点整个往前拖（实测 window=30 的并集有 55 天，实际只有 30）。
        for stock, payload in raw.items():
            if stock == "ErrorId" or not isinstance(payload, dict):
                continue
            scanned += 1
            for line in payload.values():
                for p in _line_points(line if isinstance(line, (list, tuple)) else [line]):
                    if p["date"]:
                        occ[p["date"]] = occ.get(p["date"], 0) + 1

        # 「一半以上的股票都有这根」才算窗口内的共同交易日
        common = [d for d in sorted(occ) if occ[d] * 2 >= max(1, scanned)]
        if not common:                      # 股票极少时兜底
            common = sorted(occ)
        window_start = common[0] if common else ""
        window_end = common[-1] if common else ""
        common_set = set(common)

        def _fail(reason: str) -> Dict[str, Any]:
            return {
                "ok": False,
                "reason": reason,
                "hits": [],
                "signals": {},
                "args": arg_str,
                "window": win,
                "date": "",
                "date_requested": _ymd_to_iso(target),
                "window_start": _ymd_to_iso(window_start),
                "window_end": _ymd_to_iso(window_end),
                "dates": [_ymd_to_iso(d) for d in common],
                "elapsed": round(time.perf_counter() - t0, 3),
                "scanned": scanned,
            }

        if not window_end:
            return _fail("公式没有返回任何带日期的输出点，无法确定信号日")

        # --- 目标日解析：非交易日统一吸附到前一个共同交易日 -----------
        if not target:
            used = window_end
        else:
            cands = [d for d in common if d <= target]
            if not cands:
                return _fail(
                    f"指定信号日 {_ymd_to_iso(target)} 早于公式窗口起点 "
                    f"{_ymd_to_iso(window_start)}（当前窗口 {win} 个交易日，"
                    f"{_ymd_to_iso(window_start)} ~ {_ymd_to_iso(window_end)}）；"
                    f"要回看更早的日子请把「回看窗口」调大"
                )
            used = cands[-1]
        adjusted = ""
        if target and used != target:
            adjusted = (
                f"{_ymd_to_iso(target)} 不在公式窗口的交易日里，"
                f"已吸附到不晚于它的最近交易日 {_ymd_to_iso(used)}"
            )

        # --- 第二遍：只按 used 这一天判命中 ---------------------------
        for stock, payload in raw.items():
            if stock == "ErrorId" or not isinstance(payload, dict):
                continue
            hit_line: Optional[tuple] = None
            sel: Optional[Dict[str, str]] = None
            sel_name = ""
            own_dates: set = set()
            for line_name, line in payload.items():
                pts = _line_points(line if isinstance(line, (list, tuple)) else [line])
                if not pts:
                    continue
                for p in pts:
                    if p["date"] in common_set and is_hit(p["value"]):
                        own_dates.add(p["date"])
                got = _point_on(pts, used)
                if got is None:          # 当日停牌 / 上市晚于该日 → 不算命中
                    continue
                if sel is None:
                    sel, sel_name = got, str(line_name)
                if hit_line is None and is_hit(got.get("value")):
                    hit_line = (str(line_name), got)
            for d in own_dates:
                date_hits[d] = date_hits.get(d, 0) + 1
            if hit_line is not None:
                name, got = hit_line
                hits.append(
                    {
                        "code": stock,
                        "line": name,
                        "value": got["value"],
                        "date": _ymd_to_iso(got.get("date", "")),
                    }
                )
            if sel is not None:
                signals[stock] = {
                    "line": sel_name,
                    "value": sel.get("value"),
                    "date": _ymd_to_iso(sel.get("date", "")),
                }

        return {
            "ok": True,
            "formula": code,
            "formula_name": str(info.get("acName") or code),
            "args": arg_str,
            "hits": hits,
            "signals": signals,
            "errors": errors,
            "window": win,
            # 本条链路对外**统一 ISO**（YYYY-MM-DD）：date / window_start /
            # window_end / dates / date_hits / hits[].date 都是同一个口径，
            # 不要再出现 YYYYMMDD 与 YYYY-MM-DD 混着用。
            "date": _ymd_to_iso(used),
            "date_requested": _ymd_to_iso(target),
            "date_adjusted": adjusted,
            "window_start": _ymd_to_iso(window_start),
            "window_end": _ymd_to_iso(window_end),
            "dates": [_ymd_to_iso(d) for d in common],
            "date_hits": {_ymd_to_iso(d): c for d, c in sorted(date_hits.items())},
            "total": len(hits),
            "scanned": scanned,
            "elapsed": round(time.perf_counter() - t0, 3),
        }

    # ------------------------------------------------------------------
    # 单只完整历史（用于交叉核对 / 展示信号序列）
    # ------------------------------------------------------------------
    def run_xg_series(
        self,
        code: str,
        args: Any = None,
        stock_code: str = "",
        bars: int = 250,
    ) -> Dict[str, Any]:
        """对**单只**股票跑公式，返回整条输出线（含历史信号位置）。"""
        with self._lock:
            info = self.formula_info(code, FORMULA_TYPES["xg"])
            arg_str = normalize_args(info, args)
            tq = self._connect()
            tq.formula_set_data_info(
                stock_code=stock_code,
                stock_period="1d",
                count=int(bars),
                dividend_type=self.dividend_type,
            )
            out = tq.formula_xg(formula_name=code, formula_arg=arg_str) or {}
        value = out.get("Value") or {}
        line_name = next(iter(value), "")
        line = list(value.get(line_name) or [])
        positions = [i for i, v in enumerate(line) if is_hit(v)]
        return {
            "ok": bool(value),
            "stock": stock_code,
            "formula": code,
            "args": arg_str,
            "line": line_name,
            "length": len(line),
            "hit_positions": positions,
            "hit_count": len(positions),
        }
