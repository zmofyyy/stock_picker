"""涨跌停判定（口径与交易所一致，基准价取自本地 ``.day``）。

为什么单独一个模块
------------------
判定的正确性取决于两件事：**基准收盘价**（决定涨停价）和**该标的当前的涨跌幅
限制**（10% / 20% / 30% / ST 5%）。

**基准价用的是「前一根 K 线的收盘」**。``.day`` 第 8 个 uint32 名义上是「上日
收盘价」，看起来更权威，但实测（2026-09-23 全市场抽样）**自 2015-12-16 起全部
失效** —— 沪/深约 63~67% 的行是占位值 ``65536``、约 10% 是 ``0``，只有 2015 年
及更早的数据有真值，北交所从来没有。所以它救不了近期行情，本项目一律用前一根
收盘（``tdx_reader.DAY_DTYPE`` 里有完整说明）。

涨跌幅限制不只是「代码前缀查表」：主板 ST 股的涨跌幅是 5%，而创业板 / 科创板的
ST 股仍然是 20%、北交所仍是 30% —— 所以顺序必须是「先判交易所/板块，再判 ST」。

口径（与通达信、交易所一致）
----------------------------
- 涨停价 = ``round(prev × (1 + rate))``，**用「分」整数算**：``(prev_c * f + 50) // 100``
  （``f`` = 涨跌幅 ×100，如 110）。用浮点算会在 0.01 元的边界上抖动。
- 北交所 30%；创业板 ``300/301``、科创板 ``688/689`` 20%；主板 10%；主板 ST 5%。
- 跌停额外要求「最低价 ≥ 跌停价」，用于排除除权造成的假跌停。
- **上市首日不参与判定**：首日无涨跌幅限制，涨 44% 会被误判成涨停。

已知边界（界面与文档都会注明）
------------------------------
- **本机数据的 ST 5% 并不成立**（2026-09-23 实测）：抽取 144 只名称含 ST 的主板个股，
  142 只都存在「日内最高价 > 前收 × 1.05」的交易日（占其交易日 8%~37%），``high/prev``
  的 p99.9 几乎全部落在 **1.10**。也就是说这批本地行情里主板 ST 股的涨跌幅限制**是
  10%**。因此 :meth:`MarketService.streaks` 的 ``st_limit`` **默认关闭**；套用 5% 会把
  涨幅 5%~10% 的普通交易日错判成涨停（``2026-06-01`` 一天多出 40 只）。兄弟应用
  ``stock_watch/views/streaks.py`` 也写明「ST 股按常规板近似」。
- **除权日**：前收用的是未复权的上一根收盘，除权日它会偏高（送股后价格被向下
  调整），于是涨停价算高、**可能漏判除权当天的涨停**（高送转除权当天涨停并不
  少见）。这是本地方案里唯一无法靠数据修掉的一点。
- 创业 / 科创板上市前 5 个交易日、北交所首日均无涨跌幅限制，本模块只剔除**首日**，
  所以这些标的上市第 2~5 天的大涨可能被算成涨停。
- 停牌复牌无涨跌幅限制（重大资产重组等）的当天同样可能被误判。
- ST 是按**当前名称**判定的，无法回溯「某历史日期时它是否已 ST」。
- 退市整理期股票按 10% 处理（沪深主板口径）。
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

from .tdx_reader import split_code

#: 涨跌幅因子（×100）。键只是给人类读的标签，实际取值见 :func:`limit_factor`。
F_MAIN = 110
F_GEM = 120   # 创业板
F_STAR = 120  # 科创板
F_BJ = 130
F_ST = 105

#: 因子 → 展示文案
FACTOR_LABEL = {F_ST: "5%", F_MAIN: "10%", F_GEM: "20%", F_BJ: "30%"}

#: 创业板 / 科创板代码前缀（20% 涨跌幅）
_TWENTY_PCT = ("300", "301", "688", "689")


def is_st_name(name: str) -> bool:
    """名称是否带风险警示标识（``ST`` / ``*ST``）。

    只判 ``ST``，**不含「退」** —— 退市整理期股票的涨跌幅是 10%（不是 5%），
    两者的用途不同，别合并。选股里的「剔除 ST/退市」请用
    :func:`is_st_or_delisting`。
    """
    if not name:
        return False
    return "ST" in str(name).upper().replace(" ", "")


def is_st_or_delisting(name: str) -> bool:
    """名称是否属于「ST 或退市」——选股过滤用的口径。"""
    if not name:
        return False
    return is_st_name(name) or "退" in str(name)


def limit_factor(code: str, name: str = "", st_limit: bool = True) -> int:
    """该标的的涨跌幅因子（×100）。

    :param code: ``600000.SH`` / ``sh600000`` / ``600000`` 均可
    :param name: 股票名称；用于识别 ST（主板才是 5%）
    :param st_limit: 关掉则一律按常规板处理（想与只看代码前缀的口径对照时用）
    """
    symbol, market = split_code(code)
    if market == "bj":
        return F_BJ
    if symbol.startswith(_TWENTY_PCT):
        return F_GEM   # 创业板与科创板同为 20%，ST 也一样
    if st_limit and is_st_name(name):
        return F_ST
    return F_MAIN


def factor_label(factor: int) -> str:
    """``110`` → ``"10%"``（未知因子回退成 ``"±x%"``）。"""
    return FACTOR_LABEL.get(int(factor), f"±{int(factor) - 100}%")


def _to_cent(x) -> np.ndarray:
    """「元」→「分」整数（始终返回 1 维数组，标量入参也安全）。

    缓存里的价格是 float32「元」，源数据是「分」网格。float32 在最大收盘价
    （3275 元）处的 ULP 是 2.4e-4，远小于 0.005，所以 ``rint(x*100)`` 能**精确**
    还原出原始的整数分，这一步不引入误差。
    """
    a = np.asarray(x, dtype="float64")
    a = np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0)
    return np.rint(np.atleast_1d(a) * 100.0).astype("int64")


def limit_prices(prev_close, factor) -> Tuple[np.ndarray, np.ndarray]:
    """给定基准收盘价，返回 ``(涨停价, 跌停价)`` —— 单位是「**分**」整数。

    刻意返回整数分而不是元：判定时应与同样转成整数分的 close/high/low 直接比较，
    这样 0.01 元的边界不依赖浮点相等。
    """
    pc = np.asarray(prev_close, dtype="float64")
    prev_valid = np.isfinite(pc) & (pc > 0)
    prev_c = _to_cent(np.where(prev_valid, pc, 0.0))
    f = np.asarray(factor, dtype="int64")
    lup = (prev_c * f + 50) // 100            # 涨停价（分）
    ldp = (prev_c * (200 - f) + 50) // 100    # 跌停价（分）
    return lup, ldp


def limit_masks(
    close,
    high,
    low,
    prev_close,
    factor,
    is_ipo: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """向量化算出 ``(涨停, 触板未封, 跌停)`` 三个布尔掩码。

    :param close/high/low: 单位「元」，与 ``factor`` 等长的数组
    :param prev_close: 单位「元」的**基准收盘价**（即前一根 K 线的收盘）；
        允许 NaN —— 该行直接不判定（例如一只股票的上市首日，没有前收）
    :param factor: 涨跌幅因子（×100），标量或等长数组
    :param is_ipo: 上市首日掩码；True 的行不参与判定
    """
    close_c = _to_cent(close)
    high_c = _to_cent(high)
    low_c = _to_cent(low)
    lup, ldp = limit_prices(prev_close, factor)

    pc = np.asarray(prev_close, dtype="float64")
    base = np.isfinite(pc) & (pc > 0)
    if is_ipo is not None:
        base = base & ~np.asarray(is_ipo, dtype=bool)

    limit_up = base & (close_c >= lup)
    limit_down = base & (close_c <= ldp) & (low_c >= ldp)
    touched_up = base & (high_c >= lup) & ~limit_up
    return limit_up, touched_up, limit_down


#: 涨停的「封板形态」，按强度从高到低。用整数分比较，不依赖浮点相等。
SEAL_ONE_WORD = "一字板"    # 全天最低价 = 涨停价（开盘即封、从未打开）
SEAL_T_SHAPE = "T字板"      # 开盘即涨停但盘中打开过
SEAL_NORMAL = "换手板"      # 收盘涨停但开盘价低于涨停价（盘中拉板）


def seal_shape(limit_up, open_, low, prev_close, factor) -> np.ndarray:
    """给每个涨停行打上封板形态标签（非涨停行给空串）。

    一字板最强（买不进）、T 字板次之、换手板最弱 —— 连板梯队里这是判断
    「明天还能不能继续」最直接的一眼信息。
    """
    lu = np.asarray(limit_up, dtype=bool)
    lup, _ = limit_prices(prev_close, factor)
    low_c = _to_cent(low)
    open_c = _to_cent(open_)
    out = np.full(lu.shape, "", dtype=object)
    one_word = lu & (low_c >= lup)
    t_shape = lu & ~one_word & (open_c >= lup)
    out[lu] = SEAL_NORMAL
    out[t_shape] = SEAL_T_SHAPE
    out[one_word] = SEAL_ONE_WORD
    return out


def streak_length(flags) -> int:
    """从序列**末尾**往回数连续 True 的个数（首板 = 1，当日未涨停 = 0）。

    ``flags`` 需按日期升序。用于「连板数 = 截至某日连续涨停天数」。
    """
    f = np.asarray(flags, dtype=bool)
    if f.size == 0 or not f[-1]:
        return 0
    rev = f[::-1]
    bad = np.flatnonzero(~rev)
    return int(bad[0]) if bad.size else int(f.size)


def streak_series(flags) -> np.ndarray:
    """逐日给出「截至当日的连续涨停天数」（首板 1，未涨停 0）。

    等价于对每个位置调用 :func:`streak_length`，但只遍历一次。
    """
    f = np.asarray(flags, dtype=bool)
    out = np.zeros(f.size, dtype="int32")
    run = 0
    for i, v in enumerate(f):
        run = run + 1 if v else 0
        out[i] = run
    return out
