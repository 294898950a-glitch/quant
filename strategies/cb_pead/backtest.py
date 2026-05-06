"""
PEAD 回测引擎
买深幅下修转债，持有60天，等权组合。
"""
import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import config

# ============================================================
# 数据加载
# ============================================================
def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """加载日度价格序列 + 事件摘要"""
    series = pd.read_csv(config.SERIES_CSV)
    series["meeting_date"] = pd.to_datetime(series["meeting_date"])
    
    events = pd.read_csv(config.EVENTS_CSV)
    events["meeting_date"] = pd.to_datetime(events["meeting_date"])
    events["ratio"] = events["after_price"] / events["before_price"]
    
    return series, events

# ============================================================
# 持仓结构
# ============================================================
@dataclass
class Position:
    bond_id: str
    name: str
    entry_date: pd.Timestamp
    entry_price: float
    ratio: float
    exit_date: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None

@dataclass
class PortfolioState:
    cash: float = 1_000_000.0
    positions: Dict[str, Position] = field(default_factory=dict)
    equity_curve: List[Dict] = field(default_factory=list)

# ============================================================
# 回测引擎
# ============================================================
def run_backtest(series_df: pd.DataFrame, events_df: pd.DataFrame) -> pd.DataFrame:
    """
    核心回测逻辑
    
    参数:
        series_df: 日度价格序列 (T+0~T+60)
        events_df: 事件摘要 (含 ratio, is_deep)
    """
    # 筛选大幅下修
    deep_events = events_df[events_df["ratio"] <= config.RATIO_THRESHOLD].copy()
    deep_events = deep_events.sort_values("meeting_date").reset_index(drop=True)
    
    print(f"回测事件数: {len(deep_events)}")
    print(f"时间范围: {deep_events['meeting_date'].min().date()} ~ {deep_events['meeting_date'].max().date()}")
    
    # 构建交易日历 (从第一个事件的 meeting_date 到最后一个事件 T+60)
    all_dates = []
    for _, evt in deep_events.iterrows():
        md = evt["meeting_date"]
        for t in range(config.HOLD_DAYS + 1):
            all_dates.append(md + pd.Timedelta(days=t))
    calendar = sorted(set(all_dates))
    
    # 日度价格查找表: {(bond_id, meeting_date): {offset: price}}
    price_lookup = {}
    for _, row in series_df.iterrows():
        key = (str(row["bond_id"]), row["meeting_date"])
        prices = {}
        for t in range(config.HOLD_DAYS + 1):
            col = f"T+{t}"
            if col in row.index and pd.notna(row[col]):
                prices[t] = float(row[col])
        price_lookup[key] = prices
    
    # 回测
    state = PortfolioState()
    trades = []
    
    for date in calendar:
        # === 1. 检查到期退出 ===
        expired = []
        for bid, pos in state.positions.items():
            if pos.exit_date is not None:
                continue
            days_held = (date - pos.entry_date).days
            if days_held > config.HOLD_DAYS:
                key = (bid, pos.entry_date)
                prices = price_lookup.get(key, {})
                if config.HOLD_DAYS in prices:
                    pos.exit_price = prices[config.HOLD_DAYS]
                    pos.exit_date = date
                    pos.exit_reason = "hold_end"
                    trades.append({
                        "bond_id": bid, "name": pos.name,
                        "entry_date": pos.entry_date, "exit_date": date,
                        "entry_price": pos.entry_price, "exit_price": pos.exit_price,
                        "days_held": days_held, "return": pos.exit_price / pos.entry_price - 1,
                        "exit_reason": pos.exit_reason,
                    })
                    expired.append(bid)
        
        for bid in expired:
            del state.positions[bid]
        
        # === 2. 检查止损 ===
        for bid, pos in list(state.positions.items()):
            if pos.exit_date is not None:
                continue
            if config.STOP_LOSS:
                days_held = (date - pos.entry_date).days
                key = (bid, pos.entry_date)
                prices = price_lookup.get(key, {})
                if days_held in prices:
                    current = prices[days_held]
                    if current / pos.entry_price - 1 <= config.STOP_LOSS:
                        pos.exit_price = current
                        pos.exit_date = date
                        pos.exit_reason = "stop_loss"
                        trades.append({
                            "bond_id": bid, "name": pos.name,
                            "entry_date": pos.entry_date, "exit_date": date,
                            "entry_price": pos.entry_price, "exit_price": pos.exit_price,
                            "days_held": days_held, "return": pos.exit_price / pos.entry_price - 1,
                            "exit_reason": pos.exit_reason,
                        })
                        del state.positions[bid]
        
        # === 3. 新开仓 ===
        new_events = deep_events[deep_events["meeting_date"] == date]
        for _, evt in new_events.iterrows():
            if len(state.positions) >= config.MAX_POSITIONS:
                break
            bid = str(evt["bond_id"])
            key = (bid, evt["meeting_date"])
            prices = price_lookup.get(key, {})
            if 0 not in prices:
                continue
            entry_price = prices[0] * (1 + config.TOTAL_TC)  # 买入成本
            state.positions[bid] = Position(
                bond_id=bid,
                name=evt.get("name", ""),
                entry_date=evt["meeting_date"],
                entry_price=entry_price,
                ratio=evt["ratio"],
            )
        
        # === 4. 计算当日权益 ===
        active_value = 0.0
        active_positions = [p for p in state.positions.values() if p.exit_date is None]
        
        if active_positions:
            pos_value = state.cash / len(active_positions)
            for pos in active_positions:
                key = (pos.bond_id, pos.entry_date)
                prices = price_lookup.get(key, {})
                days_held = (date - pos.entry_date).days
                if days_held in prices:
                    active_value += pos_value / pos.entry_price * prices[days_held]
                else:
                    active_value += pos_value  # 无价格，保持原值
        
        state.equity_curve.append({
            "date": date,
            "equity": active_value if active_positions else state.cash,
            "n_positions": len(active_positions),
            "cash": state.cash,
        })
    
    # 构建结果
    equity_df = pd.DataFrame(state.equity_curve)
    equity_df["daily_return"] = equity_df["equity"].pct_change()
    
    trades_df = pd.DataFrame(trades) if trades else pd.DataFrame()
    
    return equity_df, trades_df

# ============================================================
# 绩效分析
# ============================================================
def analyze_performance(equity_df: pd.DataFrame, trades_df: pd.DataFrame):
    """计算绩效指标"""
    returns = equity_df["daily_return"].dropna()
    if len(returns) == 0:
        print("  ⚠️ 无有效收益数据")
        return
    
    total_return = equity_df["equity"].iloc[-1] / equity_df["equity"].iloc[0] - 1
    annualized = (1 + total_return) ** (252 / max(len(returns), 1)) - 1
    vol = returns.std() * np.sqrt(252)
    sharpe = (returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0
    max_dd = (equity_df["equity"] / equity_df["equity"].cummax() - 1).min()
    
    print(f"\n{'='*60}")
    print(f"PEAD 策略绩效")
    print(f"{'='*60}")
    print(f"  总收益:      {total_return:+.2%}")
    print(f"  年化收益:    {annualized:+.2%}")
    print(f"  年化波动:    {vol:.2%}")
    print(f"  Sharpe:      {sharpe:.2f}")
    print(f"  最大回撤:    {max_dd:.2%}")
    print(f"  交易天数:    {len(returns)}")
    
    if not trades_df.empty:
        print(f"\n--- 交易明细 ---")
        print(f"  总交易数:    {len(trades_df)}")
        win_rate = (trades_df["return"] > 0).mean()
        avg_ret = trades_df["return"].mean()
        avg_win = trades_df[trades_df["return"] > 0]["return"].mean()
        avg_loss = trades_df[trades_df["return"] < 0]["return"].mean()
        print(f"  胜率:        {win_rate:.1%}")
        print(f"  平均收益:    {avg_ret:+.2%}")
        print(f"  平均盈利:    {avg_win:+.2%}")
        print(f"  平均亏损:    {avg_loss:+.2%}")
        print(f"  盈亏比:      {abs(avg_win/avg_loss):.2f}")
        print(f"  平均持有:    {trades_df['days_held'].mean():.0f}天")
        
        if "exit_reason" in trades_df.columns:
            print(f"\n  退出原因:")
            for reason, count in trades_df["exit_reason"].value_counts().items():
                ret = trades_df[trades_df["exit_reason"] == reason]["return"].mean()
                print(f"    {reason}: {count}笔, 均收益 {ret:+.2%}")
    
    return {
        "total_return": total_return,
        "annualized": annualized,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "n_trades": len(trades_df),
        "win_rate": win_rate if not trades_df.empty else 0,
    }


# ============================================================
# 主程序
# ============================================================
if __name__ == "__main__":
    print("加载数据...")
    series_df, events_df = load_data()
    
    print(f"  {len(series_df)} 条日度序列")
    print(f"  {len(events_df)} 个事件")
    
    print("\n运行回测...")
    equity_df, trades_df = run_backtest(series_df, events_df)
    
    metrics = analyze_performance(equity_df, trades_df)
    
    # 保存
    equity_df.to_parquet(config.OUTPUT_DIR / "equity_curve.parquet", index=False)
    if not trades_df.empty:
        trades_df.to_parquet(config.OUTPUT_DIR / "trades.parquet", index=False)
    print(f"\n📁 结果保存到 {config.OUTPUT_DIR}")
