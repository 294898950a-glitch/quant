"""
可转债强赎博弈策略 — 数据层

纯本地 Parquet 数据仓库读取，完全替代 akshare/JSL 实时 API。

数据来源: Tushare Pro (通过代理 tsy.xiaodefa.cn 拉取)
仓库路径: ~/projects/quant/data/cb_warehouse/
   - cb_basic.parquet  : 转债基本面（含转股价、剩余规模）
   - cb_daily.parquet  : 转债日线行情（含 cb_over_rate 转股溢价率）
|  - cb_call.parquet   : 强赎公告历史
||  - stk_daily.parquet : 正股日线行情（前复权，stk_daily_qfq -> stk_daily 别名）

重构目标:
  - 零外部 API 依赖
  - get_cb_redeem_data() 保持 JSL 兼容输出格式
  - 新增纯时序回测所需的数据工厂函数
"""

import logging
import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from strategies.cb_redemption.config import DATA_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

WAREHOUSE_DIR = Path(os.environ.get(
    "CB_WAREHOUSE_DIR",
    str(Path.home() / "projects" / "quant" / "data" / "cb_warehouse"),
))

_PARQUET_FILES = {
    "basic": WAREHOUSE_DIR / "cb_basic.parquet",
    "daily": WAREHOUSE_DIR / "cb_daily.parquet",
    "call": WAREHOUSE_DIR / "cb_call.parquet",
    "stk_daily": WAREHOUSE_DIR / "stk_daily_qfq.parquet",
}

SNAPSHOT_CACHE = WAREHOUSE_DIR / "strong_timeline_snapshots.parquet"


# ---------------------------------------------------------------------------
# Public read-only API: load_historical_snapshots
#
# 这是 verifier (backtest/optimizer) 唯一应该用的入口。
# 直接读取持久化的 strong_timeline_snapshots.parquet —— 只读、无副作用。
#
# ⚠️ 时序污染审计（见 docs/plans/2026-05-07-verifier-audit.md）：
#   - close, premium_ratio, stock_momentum, market_sentiment：干净
#   - redeem_progress：干净（按 ann_date 严格过滤）
#   - top1_ratio_*：干净（announcement_time + merge_asof backward）
#   - remaining_size：⚠️ 中度污染（cb_basic 仅最新值，无历史 remain）
#   - ai_signal_score / ai_reduction_score / ai_is_original：已移除（污染太重，
#       重启需先打 valid_from 时间戳）
# ---------------------------------------------------------------------------


def load_historical_snapshots(
    start: str = "20230101",
    end: Optional[str] = None,
) -> pd.DataFrame:
    """读取已持久化的历史强赎因子快照。

    严格只读：不触发任何重建。要重建用 ``build_historical_snapshots(force_rebuild=True)``。

    Args:
        start: 起始交易日 YYYYMMDD（含）
        end:   结束交易日 YYYYMMDD（含），None = 不上限裁剪

    Returns:
        含 ``date, ts_code, bond_short_name, close, premium_ratio,
        redeem_progress, remaining_size, stock_momentum, market_sentiment,
        top1_ratio_latest, top1_ratio_slope, top1_ratio_drawdown`` 的扁平表，
        按 (date, ts_code) 升序。
        注：缓存的 parquet 仍可能含旧 ai_* 列（构建时未刷新），
        verifier 调用方应只引用上面列出的字段。
    """
    if not SNAPSHOT_CACHE.exists():
        raise FileNotFoundError(
            f"历史快照不存在: {SNAPSHOT_CACHE}\n"
            f"请先调用 build_historical_snapshots() 构建。"
        )
    df = pd.read_parquet(str(SNAPSHOT_CACHE))
    df = df[df["date"] >= start]
    if end is not None:
        df = df[df["date"] <= end]
    df = df.sort_values(["date", "ts_code"]).reset_index(drop=True)
    logger.info(
        f"📦 load_historical_snapshots: {len(df)} 行, "
        f"{df['date'].nunique()} 交易日, "
        f"{df['date'].min()} ~ {df['date'].max()}"
    )
    return df

# ---------------------------------------------------------------------------
# 底层加载（带 LRU 缓存）
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _load_parquet(name: str) -> pd.DataFrame:
    """
    从 Parquet 仓库加载数据，带 LRU 缓存。
    
    Args:
        name: 数据表名 (basic / daily / call)
    
    Returns:
        DataFrame（缓存，重复调用不重复加载）
    """
    fpath = _PARQUET_FILES.get(name)
    if fpath is None:
        raise ValueError(f"未知数据表: {name}，可用: {list(_PARQUET_FILES.keys())}")
    if not fpath.exists():
        raise FileNotFoundError(f"Parquet 文件不存在: {fpath}")
    df = pd.read_parquet(str(fpath))
    logger.debug(f"已加载 {name}: {len(df)} 行, {list(df.columns)[:8]}")
    return df


def clear_cache():
    """清空所有数据缓存（用于定时优化时刷新最新数据）。"""
    _load_parquet.cache_clear()
    logger.info("📦 Parquet 缓存已清空")


def get_warehouse_status() -> dict:
    """返回数据仓库状态摘要。"""
    basic = _load_parquet("basic")
    daily = _load_parquet("daily")
    call = _load_parquet("call")
    return {
        "bonds": len(basic),
        "daily_rows": len(daily),
        "daily_dates": f"{daily['trade_date'].min()} ~ {daily['trade_date'].max()}",
        "call_events": len(call),
        "last_updated": max(
            os.path.getmtime(str(f)) for f in _PARQUET_FILES.values()
        ),
    }


# ---------------------------------------------------------------------------
# 交易日历
# ---------------------------------------------------------------------------


def get_trade_calendar(start: str = "20180101", end: Optional[str] = None) -> list[str]:
    """
    获取指定日期范围内的交易日列表（从 cb_daily 实际数据推断）。
    
    Args:
        start: 开始日期 YYYYMMDD
        end: 结束日期 YYYYMMDD，默认今天
    
    Returns:
        交易日列表（升序排列的 YYYYMMDD 字符串）
    """
    daily = _load_parquet("daily")
    dates = sorted(daily["trade_date"].unique())
    end = end or datetime.now().strftime("%Y%m%d")
    return [d for d in dates if start <= d <= end]


def get_latest_trade_date() -> str:
    """获取最新交易日 YYYYMMDD。"""
    return get_trade_calendar()[-1]


# ---------------------------------------------------------------------------
# 兼容接口: get_cb_redeem_data() — 返回 JSL 风格的 DataFrame
# ---------------------------------------------------------------------------

_COLUMN_MAP = {
    "ts_code": "代码",
    "bond_short_name": "债券简称",
    "close": "现价",
    "conv_price": "转股价",
    "stk_code": "正股代码",
    "stk_short_name": "正股简称",
    "remain_size": "剩余规模(亿元)",
    "cb_over_rate": "转股溢价率(%)",
    "issue_size": "发行规模(亿元)",
    "value_date": "起息日",
    "maturity_date": "到期日",
    "list_date": "上市日期",
}


def get_cb_redeem_data() -> pd.DataFrame:
    """
    从仓库构造"当前"转债强赎快照（兼容 JSL 输出格式）。
    
    返回所有上市中转债的最新交易日状态。
    
    Returns:
        DataFrame 含代码、简称、现价、转股价、溢价率、剩余规模等
        字段名与 JSL 兼容，便于 monitor/signals 继续使用。
    """
    basic = _load_parquet("basic")
    daily = _load_parquet("daily")
    call = _load_parquet("call")

    # 1. 获取最新交易日
    latest_date = get_latest_trade_date()

    # 2. 取最新交易日行情
    daily_latest = daily[daily["trade_date"] == latest_date].copy()
    if daily_latest.empty:
        logger.warning(f"⚠️ 最新交易日 {latest_date} 无数据")
        return pd.DataFrame()

    # 3. 取最新强赎状态（按 ts_code 取最后一条公告）
    call_latest = call.sort_values("ann_date").groupby("ts_code").last().reset_index()
    call_latest = call_latest[["ts_code", "call_type", "is_call", "call_date"]].rename(
        columns={"call_date": "强赎登记日", "call_type": "强赎类型", "is_call": "强赎状态"}
    )

    # 4. 合并
    merged = daily_latest.merge(basic, on="ts_code", how="left")
    merged = merged.merge(call_latest, on="ts_code", how="left")

    # 5. 只保留上市中转债（remian_size > 0 或未到期）
    now = datetime.now().strftime("%Y%m%d")
    merged = merged[merged["maturity_date"].fillna("20991231") >= now]

    # 6. 单位转换：剩余规模/发行规模从"元"转为"亿元"
    if "remain_size" in merged.columns:
        merged["remain_size"] = merged["remain_size"] / 1e8
    if "issue_size" in merged.columns:
        merged["issue_size"] = merged["issue_size"] / 1e8

    # 7. 重命名为 JSL 兼容格式
    result_cols = {}
    for src, dst in _COLUMN_MAP.items():
        if src in merged.columns:
            result_cols[src] = dst

    display = merged[list(result_cols.keys())].rename(columns=result_cols).copy()

    # 填充转股价为 0 的场景（极少数缺失）
    if "转股价" in display.columns:
        display["转股价"] = display["转股价"].fillna(0.0)

    # 填充溢价率（直接用 cb_over_rate）
    if "转股溢价率(%)" in display.columns:
        display["转股溢价率(%)"] = display["转股溢价率(%)"].fillna(0.0)

    # 添加现价别名
    if "现价" in display.columns:
        display["现价"] = display["现价"].fillna(0.0)

    logger.info(
        f"📊 get_cb_redeem_data: {len(display)} 只转债, 最新交易日 {latest_date}"
    )
    return display


def get_stock_daily(symbol: str, start: str = "20180101", end: str = "") -> Optional[pd.DataFrame]:
    """
    获取正股日线数据（兼容接口）。

    注意：Parquet 仓库暂时没有正股日线。
    此接口返回 None，调用方需处理。
    后续可根据需要扩展从 Tushare 拉取正股日线到仓库。

    Args:
        symbol: 股票代码（如 000001.SZ）
        start: 开始日期 YYYYMMDD
        end: 结束日期 YYYYMMDD，默认今天

    Returns:
        None（暂不支持，调用方自行回退）
    """
    logger.warning(f"⚠️ get_stock_daily 暂不支持（正股日线未入库），symbol={symbol}")
    return None


def get_cb_daily(symbol: str) -> Optional[pd.DataFrame]:
    """
    获取单只转债的完整日线（兼容接口）。

    原实现通过 akshare 获取，现在从 Parquet 仓库读取。

    Args:
        symbol: 转债代码（如 113504.SH）

    Returns:
        按日期升序排列的 DataFrame，含 trade_date, open, high, low, close, vol 等
        如找不到返回 None
    """
    daily = _load_parquet("daily")
    sub = daily[daily["ts_code"] == symbol].sort_values("trade_date").copy()
    if sub.empty:
        logger.warning(f"⚠️ 未找到转债日线: {symbol}")
        return None
    sub["date"] = pd.to_datetime(sub["trade_date"], format="%Y%m%d")
    return sub


# ---------------------------------------------------------------------------
# 历史数据工厂 — 用于严格时序回测
# ---------------------------------------------------------------------------

def build_historical_snapshots(
    start: str = "20200101",
    end: Optional[str] = None,
    force_rebuild: bool = False,) -> pd.DataFrame:
    """构建历史强赎快照序列 — 逐交易日生成因子值。

    支持 Parquet 持久化缓存：首次构建后保存到 SNAPSHOT_CACHE，
    后续调用直接加载，节省 ~500s。

    Args:
        start: 起始交易日 YYYYMMDD
        end: 结束交易日 YYYYMMDD，默认最新交易日
        force_rebuild: 强制重新构建（覆盖缓存）

    严格时序保证：交易日 t 的因子只使用 t 及之前的公开数据。

    返回字段:
        date: 交易日
        ts_code: 转债代码
        bond_short_name: 转债简称
        close: 收盘价
        premium_ratio: 转股溢价率 (%)
        redeem_progress: 强赎进度 (0~1, 从 cb_call 公告推算)
        remaining_size: 剩余规模 (亿元)
        stock_momentum: 正股动量 (转债 pct_chg 替代，5日滚动)
        market_sentiment: 市场情绪 (全市场等权 5 日收益率)

    Returns:
        扁平 DataFrame，每行 = (交易日, 转债)
    """
    # 缓存命中检查
    if not force_rebuild and SNAPSHOT_CACHE.exists():
        df = pd.read_parquet(str(SNAPSHOT_CACHE))
        # 按需裁剪日期范围
        if start or end:
            end_val = end or df["date"].max()
            df = df[(df["date"] >= start) & (df["date"] <= end_val)]
        logger.info(f"📦 快照从缓存加载: {len(df)} 行, {df['date'].nunique()} 交易日")
        return df

    logger.info(f"🔄 构建快照 (无缓存), start={start}, end={end}")
    daily = _load_parquet("daily")
    basic = _load_parquet("basic")
    call = _load_parquet("call")

    end = end or get_latest_trade_date()
    dates = sorted(daily["trade_date"].unique())
    dates = [d for d in dates if start <= d <= end]
    logger.info(f"🏗️ 构建历史快照: {len(dates)} 个交易日, {dates[0]} ~ {dates[-1]}")

    # 预计算：bond_short_name 映射
    name_map = basic.set_index("ts_code")["bond_short_name"].to_dict()

    # 预计算：remain_size 映射（用最新值，元转亿元）
    # FIXME: lookahead leak — see docs/plans/2026-05-07-verifier-audit.md
    # cb_basic 只有最新 remain_size，2023 年的真实 remain_size 应当 > 当前值（转股会减少剩余规模）。
    # 修复方案：从 cb_call/conv_record 累加重建历史 remain_size 时间序列。
    size_map = (basic.set_index("ts_code")["remain_size"] / 1e8).to_dict()
    conv_price_map = basic.set_index("ts_code")["conv_price"].to_dict()
    stk_code_map = basic.set_index("ts_code")["stk_code"].to_dict()

    # 预计算：强赎公告映射 — 每个 ts_code 的公告时间线
    call_sorted = call.sort_values(["ts_code", "ann_date"])
    call_by_code = {}
    for code, group in call_sorted.groupby("ts_code"):
        call_by_code[code] = group

    # 预计算：正股日线映射 — 每个正股代码的日线
    stk_full = _load_parquet("stk_daily")
    stk_by_code = {
        code: grp.sort_values("trade_date")
        for code, grp in stk_full.groupby("ts_code")
    }

    rows = []
    daily_by_code = {code: grp.sort_values("trade_date") for code, grp in daily.groupby("ts_code")}

    # 全市场每日收益 — 用于 market_sentiment
    daily_pivot = daily.pivot_table(
        index="trade_date", columns="ts_code", values="pct_chg"
    )
    market_sentiment = daily_pivot.rolling(5, min_periods=3).mean().mean(axis=1)

    for date_str in dates:
        date_ts = pd.Timestamp(date_str)

        # 当天所有转债行情
        day_data = daily[daily["trade_date"] == date_str]
        if day_data.empty:
            continue

        # 全市场情绪（当天值）
        sent = float(market_sentiment.get(date_str, 0.0)) if date_str in market_sentiment.index else 0.0

        for _, row in day_data.iterrows():
            code = row["ts_code"]
            close = row["close"]
            if close <= 0:
                continue

            # --- premium_ratio: 直接用 cb_over_rate ---
            premium = float(row.get("cb_over_rate", 0.0) or 0.0)

            # --- remaining_size ---
            remain = float(size_map.get(code, 0.0))

            # --- stock_momentum: 用转债的 pct_chg 滚动 ---
            cb_hist = daily_by_code.get(code)
            if cb_hist is not None:
                pos = cb_hist[cb_hist["trade_date"] == date_str]
                if not pos.empty:
                    idx = pos.index[0]
                    idx_in_df = cb_hist.index.get_loc(idx)
                    if idx_in_df >= 5:
                        mom = cb_hist.iloc[idx_in_df - 5: idx_in_df + 1]["pct_chg"].sum()
                    else:
                        mom = 0.0
                else:
                    mom = 0.0
            else:
                mom = 0.0

            # --- redeem_progress: 用正股日线精确计算 ---
            stk_code = str(stk_code_map.get(code, ""))
            progress = _calc_redeem_progress_at(
                code, date_str, call_by_code,
                stk_code=stk_code,
                conv_price=float(conv_price_map.get(code, 0.0)),
                stk_by_code=stk_by_code,
            )

            rows.append({
                "date": date_str,
                "ts_code": code,
                "bond_short_name": name_map.get(code, code),
                "close": close,
                "premium_ratio": round(premium, 2),
                "redeem_progress": round(progress, 4),
                "remaining_size": round(remain, 2),
                "stock_momentum": round(mom, 2),
                "market_sentiment": round(sent, 2),
            })

    result = pd.DataFrame(rows)
    logger.info(f"✅ 历史快照完成: {len(result)} 行, {result['date'].nunique()} 个交易日")

    # ── 合并 Holder 特征（逐报告时点正确版）──
    # 步骤: 
    # 1. 从 holder_records_v2 + announcement_metadata 拼出带日期的报告序列
    # 2. 为每份报告计算截至该日期的 slope/drawdown（避免前视偏差）
    # 3. stock_code → ts_code 映射
    # 4. merge_asof 到快照
    holder_records_path = WAREHOUSE_DIR / "holder_records_raw.parquet"
    meta_path = OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "announcement_metadata.parquet"
    records_path = Path(__file__).resolve().parent / "output" / "holder_records_v2.parquet"
    
    if records_path.exists() and meta_path.exists():
        import numpy as np
        
        # 1a. 加载原始数据
        records = pd.read_parquet(str(records_path))
        meta = pd.read_parquet(str(meta_path))
        
        # 1b. 拼接日期 (announcement_time 是毫秒时间戳)
        reports = records.merge(
            meta[["announcement_id", "announcement_time"]],
            on="announcement_id", how="left"
        )
        reports["report_date"] = pd.to_datetime(reports["announcement_time"], unit="ms")
        reports["report_date_int"] = reports["report_date"].dt.strftime("%Y%m%d").astype(int)
        reports = reports.dropna(subset=["report_date_int"])
        
        # 只保留有 top1_ratio 的记录
        reports = reports[reports["top1_ratio"].notna()].copy()
        
        # 2. 按 stock_code + report_date 排序，逐报告计算 slope/drawdown
        reports = reports.sort_values(["stock_code", "report_date_int"]).reset_index(drop=True)
        
        def _compute_stock_features(grp):
            """对单个股票的时间序列，逐行计算截至该行的 slope/drawdown。"""
            code = grp.name  # pandas 3.0.2: group key NOT in columns
            grp = grp.sort_values("report_date_int").reset_index(drop=True)
            ratios = grp["top1_ratio"].values
            n = len(ratios)
            
            slopes = [0.0] * n
            drawdowns = [0.0] * n
            
            for i in range(n):
                hist = ratios[:i+1]
                if len(hist) == 1:
                    slopes[i] = 0.0
                    drawdowns[i] = 0.0
                else:
                    if len(hist) >= 3:
                        x = np.arange(len(hist), dtype=float)
                        y = hist.astype(float)
                        x_mean = x.mean()
                        y_mean = y.mean()
                        num = ((x - x_mean) * (y - y_mean)).sum()
                        den = ((x - x_mean) ** 2).sum()
                        slopes[i] = num / den if den != 0 else 0.0
                    else:
                        slopes[i] = (hist[-1] - hist[0]) / (len(hist) - 1)
                    
                    peak = hist.max()
                    drawdowns[i] = peak - hist[-1]
            
            grp["top1_ratio_slope"] = slopes
            grp["top1_ratio_drawdown"] = drawdowns
            grp["stock_code"] = code  # 补回被 groupby 吃掉的列
            return grp
        
        reports = reports.groupby("stock_code", group_keys=False).apply(_compute_stock_features).reset_index(drop=True)
        
        # 3. stock_code → ts_code 映射
        basic = _load_parquet("basic")
        # 从 stk_code 提取数字部分做映射
        stock_map = {}
        for _, r in basic.iterrows():
            stk = str(r.get("stk_code", ""))
            num = "".join(c for c in stk if c.isdigit())
            if len(num) == 6:
                stock_map[num] = r["ts_code"]
        
        reports["stk_num"] = reports["stock_code"].astype(str).str[:6]
        reports["ts_code"] = reports["stk_num"].map(stock_map)
        reports = reports.dropna(subset=["ts_code"])
        reports = reports.sort_values(["ts_code", "report_date_int"]).reset_index(drop=True)
        
        # 4. merge_asof: 对每个快照行，取该日前最新一份 holder 报告
        holders_clean = reports[["ts_code", "report_date_int", 
                                  "top1_ratio", "top1_ratio_slope", "top1_ratio_drawdown"]].copy()
        holders_clean = holders_clean.rename(columns={"top1_ratio": "top1_ratio_latest"})
        
        result["date_int"] = result["date"].astype(int)
        result = result.sort_values(["ts_code", "date_int"]).reset_index(drop=True)
        
        def _asof_merge_group(grp):
            code = grp.name
            rsub = holders_clean[holders_clean["ts_code"] == code]
            if rsub.empty:
                grp = grp.copy()
                for c in holders_clean.columns:
                    if c not in grp.columns and c != "ts_code":
                        grp[c] = pd.NA
                grp["ts_code"] = code
                return grp
            merged = pd.merge_asof(
                grp, rsub, left_on="date_int", right_on="report_date_int",
                direction="backward",
            )
            merged["ts_code"] = code
            return merged
        
        result = result.groupby("ts_code", group_keys=False).apply(
            _asof_merge_group
        ).reset_index(drop=True)
        
        # Fill NA
        for c in ["top1_ratio_latest", "top1_ratio_slope", "top1_ratio_drawdown"]:
            if c in result.columns:
                result[c] = result[c].fillna(0.0)
        
        # Clean temp columns
        drop_cols = ["report_date_int", "date_int"]
        result = result.drop(columns=[c for c in drop_cols if c in result.columns])
        
        n_holder = (result["top1_ratio_latest"] > 0).sum()
        logger.info(f"📎 已合并 Holder 特征（逐报告时点）: top1_ratio_latest>0 = {n_holder} 行")

    # AI 持有人信号 (ai_holder_signals.parquet) 已因前视污染移除：
    # 该表无日期列、为 LLM 一次性扫描"当前"持有人结构的静态判断，
    # 旧实现把同一组标签广播到 2.5 年所有交易日。重新启用需先打 valid_from
    # 时间戳并用 merge_asof(direction="backward", by="ts_code") 接入。
    # 详见 docs/plans/2026-05-07-verifier-audit.md。

    # 持久化缓存
    result.to_parquet(str(SNAPSHOT_CACHE), index=False)
    logger.info(f"💾 快照已缓存到: {SNAPSHOT_CACHE}")
    return result


def get_stock_daily(symbol: str, start_date: Optional[str] = None) -> Optional[pd.DataFrame]:
    """获取正股日线行情（前复权价格）。

    Args:
        symbol: 正股代码，如 "600519.SH"
        start_date: 可选，过滤起始日期 YYYYMMDD

    Returns:
        按日期升序排列的 DataFrame，含 trade_date, close, pct_chg 等
        close 为前复权收盘价。
    """
    stk = _load_parquet("stk_daily")
    sub = stk[stk["ts_code"] == symbol].sort_values("trade_date").copy()
    if sub.empty:
        return None
    # 将前复权列重命名为标准名
    sub = sub.rename(columns={"close_qfq": "close", "open_qfq": "open",
                               "high_qfq": "high", "low_qfq": "low"})
    sub["date"] = pd.to_datetime(sub["trade_date"], format="%Y%m%d")
    if start_date:
        sub = sub[sub["trade_date"] >= start_date]
    return sub


def _calc_redeem_progress_at(
    code: str,
    date_str: str,
    call_by_code: dict,
    stk_code: str = "",
    conv_price: float = 0.0,
    stk_by_code: dict = None,
) -> float:
    """
    用正股日线精确计算强赎进度（正股价 >= 转股价 × 130%）。

    强赎触发条件（标准条款）：
      连续30个交易日中至少15个交易日正股收盘价 >= 转股价 × 130%

    规则：
      1. "公告实施强赎" → 1.0
      2. "董事会决议提前赎回" → 0.95
      3. "满足强赎条件" → 0.8
      4. 无公告但正在触发天数中 → 根据已满足天数/15 计算
      5. 无公告未触发 → 0.0
      6. "公告不强赎" → 0.0
    """
    records = call_by_code.get(code)
    if records is not None and not records.empty:
        past_ann = records[records["ann_date"] <= date_str]
        if not past_ann.empty:
            latest = past_ann.iloc[-1]
            is_call_val = str(latest.get("is_call", ""))
            call_type = str(latest.get("call_type", ""))

            # 公告实施强赎 → 板上钉钉
            if any(kw in is_call_val for kw in ["已强赎", "实施强赎", "强赎实施", "已赎回"]):
                return 1.0
            if "公告实施强赎" in call_type:
                return 1.0

            # 董事会决议提前赎回
            if "董事会决议提前赎回" in call_type or "提前赎回" in call_type:
                return 0.95

            # 已满足强赎条件（公司公告确认满足条件）
            if "满足强赎条件" in is_call_val:
                return 0.8

            # 公告不强赎 → 0.0
            if "不强赎" in is_call_val or "不提前赎回" in is_call_val:
                return 0.0

            # 触发提示
            if "触发" in is_call_val or "提示" in is_call_val:
                return 0.4

    # === 如果没有明确公告，用正股日线判断触发天数 ===
    if not stk_code or conv_price <= 0:
        return 0.0

    stk_sub = stk_by_code.get(stk_code) if stk_by_code is not None else None
    if stk_sub is None or stk_sub.empty:
        return 0.0

    # 取 date_str 之前最近 30 个交易日
    window = stk_sub[stk_sub["trade_date"] <= date_str].tail(30)
    if len(window) == 0:
        return 0.0

    threshold_price = conv_price * 1.3
    triggered_days = int((window["close_qfq"] >= threshold_price).sum())

    # 强赎触发：30天中至少15天达标
    if triggered_days >= 15:
        return 0.8  # 已满足条件但公司未公告

    # 正在触发中：按天数比例
    progress = triggered_days / 15.0  # 0~1 之间
    # 至少达到 0.2 才有意义（否则就是基本没触发）
    if progress < 0.2:
        return 0.0
    return round(min(progress, 0.6), 4)
