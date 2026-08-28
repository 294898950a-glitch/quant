"""转股权定价 v2 —— 把发行人强赎决策做成内生变量.

背景 / 范围 / 明确排除的东西 / 验收标准, 见
`docs/2026-08-28-cb-embedded-call-lattice-pricer-spec.txt`。这里只写"怎么实现的"。

## 跟 cb_pricer.py 的关系

`price_cb`(旧)把"是否已强赎"当外部布尔量, 满足条件但还没公告的债仍按不可赎回
的欧式 call 定价 —— `study/implied_vol_diagnostic/REPORT.txt` 第 4b 节证实这批债
被系统性定价过高(+17% vs 未接近强赎组 -5.44%)。

`price_cb_lattice`(本模块)不改这个决策, 而是把"满足条件后大概率很快被收场"
做成定价过程本身要算的一部分, 不再假设期权能活到名义到期日。

复用 `cb_pricer.py` 的 `bond_floor_pv` / `DEFAULT_CREDIT_SPREAD_BP` /
`REDEMPTION_LOCKED_VALUE` / `NEAR_MATURITY_DAYS` / `PUTABLE_PERIOD_DAYS` /
`_years_between` / `_days_between`, 不重复实现; "是否满足强赎条件"这件事
完全交给 `call_condition.py`, 本模块不自己判断。

## 用蒙特卡洛, 不用状态增强二叉树 —— 为什么

spec 里把"状态增强树 vs 蒙特卡洛"列为实现阶段的工程判断, 不预设架构。这里
选蒙特卡洛, 原因是一个精确性问题, 不是偷懒:

"过去 30 天里有几天达标"这个计数, 每往前走一天要把"30 天前那一天算不算数"
减掉。在一棵会合并同价格节点的树里, 同一个节点可能被好几条不同历史路径走到,
这些路径 30 天前发生的事很可能不一样 —— 只用一个 0-30 的计数, 不知道该减掉
哪一天, 状态转移只能近似。蒙特卡洛按路径模拟, 每条路径自己的过去 30 天历史
是确定的(`call_condition.TrailingTriggerCounter` 就是干这个的), 这一点上是
精确的。代价是蒙特卡洛有统计误差(路径数越多误差越小), 树是精确到格子间距的
确定性数值解 —— 用蒙特卡洛的模型误差换树的精确性误差, 这是本模块的选择。

## 怎么算(直接算整只债的期望现值, 不走"债底+期权"两块相加再拼接)

对每条模拟路径:

1. 从估值日真实历史里取最近 `window-1` 天的 S/K, 接上模拟出来的未来路径,
   用 `call_condition` 同一套规则滚动判断"这一天是否满足强赎条件"。
2. **票息**: 只发到这条路径实际存活的那一刻为止 —— 若在到期前第 τ 天被叫停,
   这条路径只拿到 τ 之前那几个整年的年度票息, 不拿之后那几年的票息(第一版
   实现漏了这个, 见"实现过程中的一次修正")。票息属于现金流, 按信用折现率
   折现, 跟 `bond_floor_pv` 的口径一致。
3. **终值这一步是个选择, 两条腿分开折现**: 到期(或被叫停)那一刻, 持有人在
   "拿钱"(到期本息, 或被叫停时的 `call_price`)和"转股"(`conv_ratio * S`)
   之间二选一, 挑数值大的。**哪条腿赢, 就按哪条腿的性质折现**: 拿钱赢了按
   信用折现率(这笔钱有公司信用风险), 转股赢了按无风险利率(转成股权后,
   公司信用好坏跟这笔钱没关系了)。这是简化版的 Tsiveriotis-Fernandes 拆分
   ——只在终值这一个时点做选择、按选择结果切折现率, 不是完整版那种每一步
   都动态切换的做法, 但抓住了"信用风险只该压在会变成现金的那部分"这个核心。
4. 全部路径取平均, 得到理论价。

## 实现过程中的一次修正

第一版把"到期应付本息"从 `bond_floor_pv` 里单独抠出来, 拿蒙特卡洛算出的
"提前叫停或到期"期望值替换掉, 但**整段票息仍然照抄 `bond_floor_pv` 算到名义
到期日为止**——等于不管哪天被叫停, 都照付满期票息。用"历史已经连续29天满足
强赎条件, 几乎立刻会被叫停"的合成场景测, 理论价只比旧模型低了 0.85%
(144.05 → 142.83), 跟"应该明显更低"的预期不符, 抓出了这个 bug。现在改成
按第 2 步逐路径算实际发生的票息, 同一个场景下理论价从 142.83 降到该有的水平
(重新测过, 见测试文件 `test_near_trigger_prices_lower_than_price_cb`)。

## 用蒙特卡洛, 不用状态增强二叉树 —— 为什么(不变)

spec 里把"状态增强树 vs 蒙特卡洛"列为实现阶段的工程判断, 不预设架构。这里
选蒙特卡洛, 原因是一个精确性问题, 不是偷懒:

"过去 30 天里有几天达标"这个计数, 每往前走一天要把"30 天前那一天算不算数"
减掉。在一棵会合并同价格节点的树里, 同一个节点可能被好几条不同历史路径走到,
这些路径 30 天前发生的事很可能不一样 —— 只用一个 0-30 的计数, 不知道该减掉
哪一天, 状态转移只能近似。蒙特卡洛按路径模拟, 每条路径自己的过去 30 天历史
是确定的(`call_condition.TrailingTriggerCounter` 就是干这个的), 这一点上是
精确的。代价是蒙特卡洛有统计误差(路径数越多误差越小), 树是精确到格子间距的
确定性数值解 —— 用蒙特卡洛的模型误差换树的精确性误差, 这是本模块的选择。

## 已知局限(如实列出, 不是要在这里修的问题)

- 终值折现只在"到期/被叫停那一个时点"做现金腿 vs 股权腿的切换, 不是完整
  Tsiveriotis-Fernandes 那种沿途每一步都动态切换的版本 —— 本模型只有一个
  决策点(发行人的机械触发), 不像完整版那样要处理持有人随时能自愿转股的
  情形, 所以这个简化对本模型的范围是够用的, 但如果以后加了自愿转股或回售,
  这里要重新做。
- 票息假设每年末付一次、不含被叫停时的应计利息(真实转债赎回通常会补应计
  利息, 这里没算, 会让触发较早的路径理论价略低于真实值)。
- 触发条件用 `call_condition.py` 的默认市场通行参数(30/1.30/15), 不是逐券
  真实条款。
- 下修、回售不建模(数据不支持, 见 spec 第 4 节)。
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from strategies.cb_arb.call_condition import (
    DEFAULT_REQUIRED_DAYS,
    DEFAULT_THRESHOLD,
    DEFAULT_WINDOW,
)
from strategies.cb_arb.cb_pricer import (
    CBSpec,
    CBValuation,
    DEFAULT_CREDIT_SPREAD_BP,
    NEAR_MATURITY_DAYS,
    PUTABLE_PERIOD_DAYS,
    REDEMPTION_LOCKED_VALUE,
    _days_between,
    bond_floor_pv,
)

TRADING_DAYS_PER_YEAR = 252
DEFAULT_N_PATHS = 5000


def price_cb_lattice(
    spec: CBSpec,
    valuation_date: str,
    stock_price: float,
    stock_vol: float,
    moneyness_history: np.ndarray,
    risk_free_rate: float = 0.025,
    credit_spread_bp: Optional[dict] = None,
    is_force_redeemed: bool = False,
    call_price: float = REDEMPTION_LOCKED_VALUE,
    n_paths: int = DEFAULT_N_PATHS,
    window: int = DEFAULT_WINDOW,
    threshold: float = DEFAULT_THRESHOLD,
    required: int = DEFAULT_REQUIRED_DAYS,
    seed: Optional[int] = None,
) -> CBValuation:
    """算一只 CB 在某日的理论价, 把强赎决策做成内生的.

    Args:
        spec, valuation_date, stock_price, stock_vol, risk_free_rate,
            credit_spread_bp, is_force_redeemed: 同 `cb_pricer.price_cb`。
        moneyness_history: 估值日(含)为止的真实 S/K 历史, 时间升序(最旧到最新),
            至少建议给最近 `window-1` 个交易日。**必填, 不给默认值** —— 缺这段
            历史会让模型看不到"已经很接近触发"这件事, 系统性低估触发风险,
            等于悄悄把"数据不够"answer 成"还早"。
        call_price: 触发后发行人按此价收回(每 100 面值), 默认沿用
            `cb_pricer.REDEMPTION_LOCKED_VALUE`。
        n_paths: 蒙特卡洛路径数。
        window/threshold/required: 强赎条件参数, 默认与 `call_condition.py`
            一致, 允许覆盖仅用于敏感性分析, 生产口径不应偏离默认值。
        seed: 随机种子, 传入可复现, 用于测试。

    Returns:
        CBValuation, method="lattice_mc"(或 corner case 复用旧的 method 值)。
    """
    if credit_spread_bp is None:
        credit_spread_bp = DEFAULT_CREDIT_SPREAD_BP

    # ---- 缺数据保护 (与 price_cb 一致) ----
    if (
        stock_price is None
        or (isinstance(stock_price, float) and math.isnan(stock_price))
        or stock_price <= 0
    ):
        return CBValuation(
            theoretical=float("nan"),
            bond_floor=float("nan"),
            option_value=float("nan"),
            intrinsic=float("nan"),
            method="invalid",
            notes="missing_or_invalid_stock_price",
        )

    conv_ratio = spec.face_value / spec.conv_price
    intrinsic = conv_ratio * stock_price

    # ---- corner case: 已公告强赎 -> 锁顶(跟 price_cb 完全一致的口径) ----
    if is_force_redeemed:
        return CBValuation(
            theoretical=REDEMPTION_LOCKED_VALUE,
            bond_floor=REDEMPTION_LOCKED_VALUE,
            option_value=0.0,
            intrinsic=intrinsic,
            method="redemption_locked",
            notes="force_redemption_triggered",
        )

    days_to_maturity = _days_between(valuation_date, spec.maturity_date)
    T_years = days_to_maturity / 365.25

    spread_bp = credit_spread_bp.get(spec.rating, DEFAULT_CREDIT_SPREAD_BP["AA"])
    discount_rate = risk_free_rate + spread_bp / 10000.0

    bf = bond_floor_pv(
        face_value=spec.face_value,
        coupon_rate=spec.coupon_rate,
        years_to_maturity=max(T_years, 0.0),
        discount_rate=discount_rate,
    )

    notes_extras: list[str] = []
    if 0 < days_to_maturity <= PUTABLE_PERIOD_DAYS:
        bf = max(bf, spec.face_value)
        notes_extras.append("putable_period_floor")

    # ---- corner case: 距到期 < 30 天 -> 跟 price_cb 一样走 intrinsic, 不做 MC ----
    if days_to_maturity < NEAR_MATURITY_DAYS:
        theo = max(intrinsic, bf)
        return CBValuation(
            theoretical=theo,
            bond_floor=bf,
            option_value=0.0,
            intrinsic=intrinsic,
            method="intrinsic",
            notes=";".join(["near_maturity_lt_30d"] + notes_extras),
        )

    # ---- 票息现值查表: coupon_pv_table[n] = 前 n 个年度票息(每年末付一次)
    # 的现值和, 按信用折现率折 —— 跟 bond_floor_pv 同一套贴现口径, 只是这里
    # 要按每条路径实际存活了几个整年分别取数, 不能整段一次性算死。
    coupon = spec.face_value * spec.coupon_rate
    full_years = int(math.floor(T_years))
    coupon_pv_table = np.zeros(full_years + 2)
    running = 0.0
    for k in range(1, full_years + 2):
        running += coupon / (1.0 + discount_rate) ** k
        coupon_pv_table[k] = running

    repayment_terminal = spec.face_value * (1.0 + spec.coupon_rate)

    # ---- 蒙特卡洛模拟 ----
    n_days = max(int(round(T_years * TRADING_DAYS_PER_YEAR)), 1)
    dt = 1.0 / TRADING_DAYS_PER_YEAR
    vol_safe = max(0.0, min(stock_vol if stock_vol is not None else 0.0, 5.0))

    rng = np.random.default_rng(seed)
    z = rng.standard_normal((n_paths, n_days))
    drift = (risk_free_rate - 0.5 * vol_safe * vol_safe) * dt
    diffusion = vol_safe * math.sqrt(dt) * z
    log_paths = np.cumsum(drift + diffusion, axis=1)
    S_paths = stock_price * np.exp(log_paths)  # (n_paths, n_days)

    # ---- 拼接真实历史(所有路径共用) + 模拟出的未来 ----
    seed_len = window - 1
    seed_window = np.full(seed_len, np.nan)
    hist = np.asarray(moneyness_history, dtype=float)
    if hist.size > 0:
        take = min(hist.size, seed_len)
        seed_window[seed_len - take :] = hist[-take:]
    with np.errstate(invalid="ignore"):
        seed_hit = (seed_window >= threshold).astype(float)
    seed_hit = np.nan_to_num(seed_hit, nan=0.0)

    moneyness_paths = S_paths / spec.conv_price
    with np.errstate(invalid="ignore"):
        sim_hit = (moneyness_paths >= threshold).astype(float)
    sim_hit = np.nan_to_num(sim_hit, nan=0.0)

    full_hit = np.concatenate(
        [np.tile(seed_hit, (n_paths, 1)), sim_hit], axis=1
    )  # (n_paths, seed_len + n_days)

    cumsum = np.concatenate(
        [np.zeros((n_paths, 1)), np.cumsum(full_hit, axis=1)], axis=1
    )
    roll_count = cumsum[:, window:] - cumsum[:, :-window]
    # 当 seed_len == window-1 (标准情形) 时, roll_count 的第 t 列恰好对应模拟第 t 天
    # (0-indexed)的"过去 window 天达标数"。若真实历史给的天数不够 window-1,
    # seed_window 里对应位置是 NaN -> 已按 0(不达标)处理, 结果是"数据不够时更晚
    # 判定触发", 不会更早 —— 保守方向正确, 不会凭空提前触发。
    eligible = roll_count >= required  # (n_paths, n_days)

    has_trigger = eligible.any(axis=1)
    first_trigger_idx = np.argmax(eligible, axis=1)  # 无触发时是 0, 用 has_trigger 区分

    pv_all = np.empty(n_paths, dtype=float)

    # ---- 被叫停的路径: 只拿到叫停前那几个整年的票息 + 叫停时二选一的终值 ----
    idx_triggered = np.where(has_trigger)[0]
    tau = first_trigger_idx[idx_triggered]  # 0-indexed 模拟日, 第 tau+1 个交易日触发
    S_tau = S_paths[idx_triggered, tau]
    elapsed_triggered = (tau + 1) * (T_years / n_days)  # 折算回日历年, 与 T_years 同口径
    n_coupons_triggered = np.clip(np.floor(elapsed_triggered).astype(int), 0, full_years)
    coupon_pv_triggered = coupon_pv_table[n_coupons_triggered]

    conv_val_triggered = conv_ratio * S_tau
    cash_wins_triggered = call_price >= conv_val_triggered
    terminal_pv_triggered = np.where(
        cash_wins_triggered,
        call_price * np.exp(-discount_rate * elapsed_triggered),
        conv_val_triggered * np.exp(-risk_free_rate * elapsed_triggered),
    )
    pv_all[idx_triggered] = coupon_pv_triggered + terminal_pv_triggered

    # ---- 一直没被叫停、活到名义到期日的路径: 拿满 full_years 个整年票息 + 到期二选一 ----
    idx_never = np.where(~has_trigger)[0]
    S_T = S_paths[idx_never, -1]
    coupon_pv_never = coupon_pv_table[full_years]
    conv_val_never = conv_ratio * S_T
    cash_wins_never = repayment_terminal >= conv_val_never
    terminal_pv_never = np.where(
        cash_wins_never,
        repayment_terminal * math.exp(-discount_rate * T_years),
        conv_val_never * math.exp(-risk_free_rate * T_years),
    )
    pv_all[idx_never] = coupon_pv_never + terminal_pv_never

    theoretical = float(np.mean(pv_all))
    option_value = theoretical - bf

    frac_triggered = float(np.mean(has_trigger))
    notes_extras.append(f"mc_n_paths={n_paths}")
    notes_extras.append(f"mc_frac_triggered={frac_triggered:.4f}")

    return CBValuation(
        theoretical=theoretical,
        bond_floor=bf,
        option_value=option_value,
        intrinsic=intrinsic,
        method="lattice_mc",
        notes=";".join(notes_extras),
    )
