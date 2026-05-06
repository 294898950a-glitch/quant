#!/usr/bin/env python3
"""
可转债大股东减持特征计算模块

数据流:
  PDF解析结果(holder_records.parquet) → 减持特征 → 合并到事件面板

计算逻辑:
  1. 对每只正股，按公告日排序top1_ratio时间序列
  2. 连续报告的top1_ratio变化 → 减持/增持信号
  3. 结合shd_ration_ratio(初始配售)做基准对照

用法:
  python3 -m strategies.cb_redemption.scripts.compute_holder_features
  python3 -m strategies.cb_redemption.scripts.compute_holder_features --output data/cb_warehouse/holder_features.parquet
"""

import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("holder_features")

# 路径配置
WAREHOUSE_DIR = Path.home() / "projects" / "quant" / "data" / "cb_warehouse"
STRATEGY_DIR = Path.home() / "projects" / "quant" / "strategies" / "cb_redemption"
OUTPUT_DIR = STRATEGY_DIR / "output"

# 默认输出
DEFAULT_OUTPUT = WAREHOUSE_DIR / "holder_features.parquet"


def load_holder_records() -> pd.DataFrame:
    """加载PDF解析的十大持有人记录"""
    path = OUTPUT_DIR / "holder_records_v2.parquet"
    if not path.exists():
        log.warning(f"Holder records not found at {path}")
        return pd.DataFrame()
    df = pd.read_parquet(path)
    log.info(f"Loaded {len(df)} holder records")
    return df


def load_announcement_metadata() -> pd.DataFrame:
    """加载公告元数据（含公告时间）"""
    path = OUTPUT_DIR / "announcement_metadata.parquet"
    if not path.exists():
        log.warning(f"Metadata not found at {path}")
        return pd.DataFrame()
    df = pd.read_parquet(path)
    # 提取公告日期
    if "announcement_time" in df.columns:
        df["ann_date"] = pd.to_datetime(df["announcement_time"], unit="ms")
    return df


def load_shd_ratio() -> pd.DataFrame:
    """加载初始配售比例"""
    path = WAREHOUSE_DIR / "cb_issue.parquet"
    if not path.exists():
        log.warning(f"cb_issue not found at {path}")
        return pd.DataFrame()
    df = pd.read_parquet(path)
    return df[["ts_code", "shd_ration_ratio"]].copy()


def load_call_events() -> pd.DataFrame:
    """加载强赎事件"""
    path = WAREHOUSE_DIR / "cb_call.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["ann_date"] = pd.to_datetime(df["ann_date"], format="%Y%m%d")
    return df


def load_cb_basic() -> pd.DataFrame:
    path = WAREHOUSE_DIR / "cb_basic.parquet"
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)


def compute_holder_timeseries(holders: pd.DataFrame, meta: pd.DataFrame) -> pd.DataFrame:
    """
    构建每只正股的top_ratio时间序列.
    
    holder_records每行有一条PDF的解析结果:
      - stock_code: 正股代码
      - title: 报告标题
      - top1_ratio, top3_ratio, top5_ratio, top10_ratio
      - num_holders
      
    返回: 按stock_code+ann_date排序的DataFrame
    """
    if holders.empty or meta.empty:
        return pd.DataFrame()
    
    # 合并公告日期
    merged = holders.merge(
        meta[["announcement_id", "ann_date", "category"]],
        on="announcement_id",
        how="left",
    )
    
    # 清理：删除无日期或无双数的行
    merged = merged.dropna(subset=["ann_date"])
    
    # 按正股+日期排序
    merged = merged.sort_values(["stock_code", "ann_date"])
    
    log.info(f"Holder timeseries: {len(merged)} records, "
             f"{merged['stock_code'].nunique()} stocks")
    return merged


def compute_dilution_features(
    timeseries: pd.DataFrame,
    shd_ratio: pd.DataFrame,
) -> pd.DataFrame:
    """
    从holder时间序列计算减持特征.
    
    对每只正股:
      - latest_top1_ratio: 最近一次报告的top1比例
      - earliest_top1_ratio: 最早一次报告的top1比例
      - top1_ratio_trend: 线性趋势(正=增持,负=减持)
      - top1_ratio_volatility: 相邻报告的变化幅度
      - max_drawdown: 相对历史最高的回撤
      - shd_ratio_gap: 初始配售比例 vs 最近持有比例(差值大=大股东卖出多)
      - num_reports: 有数据的报告数
      - latest_report_date: 最新报告日期
    
    每只正股输出一行（最新状态的特征向量）。
    """
    if timeseries.empty:
        return pd.DataFrame()
    
    features = []
    
    for (stk_code), grp in timeseries.groupby("stock_code"):
        grp = grp.sort_values("ann_date")
        
        f = {"stock_code": stk_code}
        
        # 基础统计
        f["num_reports"] = len(grp)
        f["latest_report_date"] = grp["ann_date"].iloc[-1]
        f["earliest_report_date"] = grp["ann_date"].iloc[0]
        
        for col in ["top1_ratio", "top3_ratio", "top5_ratio", "top10_ratio"]:
            if col not in grp.columns:
                continue
            vals = grp[col].dropna()
            if len(vals) == 0:
                continue
            
            f[f"{col}_latest"] = vals.iloc[-1]
            f[f"{col}_earliest"] = vals.iloc[0]
            f[f"{col}_mean"] = vals.mean()
            f[f"{col}_max"] = vals.max()
            f[f"{col}_min"] = vals.min()
            
            # 近期变化：最近3份 vs 之前
            if len(vals) >= 4:
                recent_mean = vals.iloc[-3:].mean()
                earlier_mean = vals.iloc[:-3].mean()
                f[f"{col}_recent_vs_prior"] = recent_mean - earlier_mean
            elif len(vals) >= 2:
                f[f"{col}_recent_vs_prior"] = vals.iloc[-1] - vals.iloc[-2]
            
            # 最大回撤
            if vals.max() > 0:
                f[f"{col}_drawdown"] = (vals.iloc[-1] - vals.max()) / vals.max() * 100
            
            # top1_ratio 线性趋势
            if col == "top1_ratio" and len(vals) >= 3:
                x = list(range(len(vals)))
                y = vals.values
                n = len(x)
                slope = (n * sum(xi * yi for xi, yi in zip(x, y)) - sum(x) * sum(y)) / \
                        (n * sum(xi * xi for xi in x) - sum(x) ** 2) if \
                        (n * sum(xi * xi for xi in x) - sum(x) ** 2) != 0 else 0
                f["top1_ratio_slope"] = slope
                
                # 稳定性：最近3份的波动
                if len(vals) >= 3:
                    recent_3 = vals.iloc[-3:]
                    f["top1_ratio_recent_std"] = recent_3.std()
        
        # 相邻报告变化幅度均值
        diffs = grp["top1_ratio"].diff().dropna()
        if len(diffs) > 0:
            f["top1_ratio_avg_change"] = diffs.abs().mean()
            f["top1_ratio_neg_changes"] = (diffs < -0.5).sum()  # 显著减持次数(>0.5%)
            f["top1_ratio_pos_changes"] = (diffs > 0.5).sum()   # 显著增持次数
        
        features.append(f)
    
    result = pd.DataFrame(features)
    log.info(f"Computed features for {len(result)} stocks from holder data")
    return result


def merge_with_events(
    features: pd.DataFrame,
    call_events: pd.DataFrame,
    cb_basic: pd.DataFrame,
    shd_ratio: pd.DataFrame,
) -> pd.DataFrame:
    """
    将减持特征合并到强赎事件面板.
    
    对每个强赎事件，取事件前最新的一期持有人数据。
    用 merge_asof 避免前视偏差。
    """
    if call_events.empty:
        log.warning("No call events to merge")
        return pd.DataFrame()
    
    # 将stock_code映射到ts_code
    if "stk_code" in cb_basic.columns:
        stock_map = {}
        for _, r in cb_basic.iterrows():
            stk = r.get("stk_code", "")
            if pd.isna(stk):
                continue
            num = re.search(r"(\d{6})", str(stk))
            if num:
                stock_map[num.group(1)] = r["ts_code"]
        
        # 给holder特征加ts_code
        if not features.empty and "ts_code" not in features.columns:
            features["stk_num"] = features["stock_code"].astype(str).str[:6]
            features["ts_code"] = features["stk_num"].map(stock_map)
            features = features.dropna(subset=["ts_code"])
    
    # 准备事件表
    events = call_events.copy()
    events["event_date"] = events["ann_date"]
    
    # 准备holder特征表（每个stock_code的最新一条）
    if not features.empty and len(features) > 0:
        holder_features = features.copy()
        holder_features["declare_date"] = pd.to_datetime(
            holder_features["latest_report_date"]
        )
        
        # merge_asof: 对每个事件，取事件前最近的holder数据
        # pandas 3.0.2 by= 参数有 bug，用 groupby-apply 绕过
        events_sorted = events.sort_values(["ts_code", "event_date"]).reset_index(drop=True)
        events_sorted["event_ts"] = pd.to_datetime(events_sorted["event_date"]).astype("int64") // 10**9
        holder_sorted = holder_features.sort_values(["ts_code", "declare_date"]).reset_index(drop=True)
        holder_sorted["declare_ts"] = pd.to_datetime(holder_sorted["declare_date"]).astype("int64") // 10**9
        
        def _asof_merge_group(grp):
            code = grp.name
            rsub = holder_sorted[holder_sorted["ts_code"] == code]
            if rsub.empty:
                grp = grp.copy()
                for c in holder_sorted.columns:
                    if c not in grp.columns and c != "ts_code":
                        grp[c] = pd.NA
                grp["ts_code"] = code
                return grp
            merged_g = pd.merge_asof(grp, rsub, left_on="event_ts", right_on="declare_ts",
                                      direction="backward")
            merged_g["ts_code"] = code
            return merged_g
        
        merged = events_sorted.groupby("ts_code", group_keys=False).apply(_asof_merge_group).reset_index(drop=True)
        # 清理临时列
        for c in ["event_ts", "declare_ts", "event_date", "declare_date"]:
            if c in merged.columns:
                merged = merged.drop(columns=[c])
        
        log.info(f"Merged features with {len(merged)} call events "
                 f"({merged['top1_ratio_latest'].notna().sum()} have holder data)")
    else:
        merged = events.copy()
    
    # 合并shd_ratio
    if not shd_ratio.empty:
        merged = merged.merge(shd_ratio, on="ts_code", how="left")
        log.info(f"Merged shd_ratio: {merged['shd_ration_ratio'].notna().sum()}/{len(merged)}")
    
    return merged


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="可转债大股东减持特征计算"
    )
    parser.add_argument(
        "--output", default=str(DEFAULT_OUTPUT),
        help=f"输出路径 (默认: {DEFAULT_OUTPUT})"
    )
    parser.add_argument(
        "--merge-events", action="store_true",
        help="合并到强赎事件面板"
    )
    args = parser.parse_args()
    
    # 加载数据
    holders = load_holder_records()
    meta = load_announcement_metadata()
    shd = load_shd_ratio()
    call = load_call_events()
    basic = load_cb_basic()
    
    if holders.empty:
        log.warning("暂无holder记录，跳过特征计算")
        print(json.dumps({"status": "skip", "reason": "no_holder_data"}))
        return
    
    # 构建时间序列
    ts = compute_holder_timeseries(holders, meta)
    
    # 计算减持特征
    features = compute_dilution_features(ts, shd)
    
    if features.empty:
        log.warning("特征计算无结果")
        return
    
    # 保存特征
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    features.to_parquet(out_path, index=False)
    log.info(f"Saved {len(features)} features to {out_path}")
    
    # 合并到事件面板（可选）
    if args.merge_events:
        merged = merge_with_events(features, call, basic, shd)
        event_path = out_path.parent / "holder_event_features.parquet"
        merged.to_parquet(event_path, index=False)
        log.info(f"Saved {len(merged)} event features to {event_path}")
    
    # 简要输出
    summary = {
        "status": "ok",
        "stocks_with_holder_data": len(features),
        "avg_top1": float(features["top1_ratio_latest"].mean()) if "top1_ratio_latest" in features else 0,
        "output": str(out_path),
    }
    print(json.dumps(summary, ensure_ascii=False))
    
    log.info("Done!")


if __name__ == "__main__":
    main()
