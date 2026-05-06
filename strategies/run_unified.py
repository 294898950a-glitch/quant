"""
统一回测入口 — PEAD + 强赎窗口套利
"""
import pandas as pd
import numpy as np
from pathlib import Path
from unified_engine import Portfolio, StrategyConfig, StrategyEvent

ROOT = Path.home() / "projects/quant"
PEAD_RAW = ROOT / "data/cb_pead/raw"
OUTPUT = ROOT / "data/cb_pead/backtest"


def load_pead_strategy() -> StrategyConfig:
    """加载 PEAD 策略"""
    events_df = pd.read_csv(PEAD_RAW / "cb_down_events_with_returns.csv")
    series_df = pd.read_csv(PEAD_RAW / "cb_pead_series.csv")
    
    events_df['meeting_date'] = pd.to_datetime(events_df['meeting_date'])
    events_df['ratio'] = events_df['after_price'] / events_df['before_price']
    series_df['meeting_date'] = pd.to_datetime(series_df['meeting_date'])
    
    # 筛选深幅下修
    deep = events_df[events_df['ratio'] <= 0.75]
    
    # 构建价格查找表
    price_lookup = {}
    for _, row in series_df.iterrows():
        bid = str(row['bond_id'])
        md = row['meeting_date']
        if bid not in deep['bond_id'].astype(str).values:
            continue
        prices = {}
        for t in range(61):
            col = f'T+{t}'
            if col in row.index and pd.notna(row[col]):
                prices[t] = float(row[col])
        price_lookup[(bid, md)] = prices
    
    # 构建事件
    events = []
    for _, row in deep.iterrows():
        events.append(StrategyEvent(
            bond_id=str(row['bond_id']),
            name=row.get('name', ''),
            event_date=row['meeting_date'],
            strategy='pead',
            hold_days=60,
            take_profit=None,   # PEAD 不限止盈
            stop_loss=-0.10,     # -10% 止损
            extra={'ratio': row['ratio']},
        ))
    
    return StrategyConfig(
        name='pead',
        events=events,
        price_lookup=price_lookup,
    )


def print_results(result: dict):
    """打印回测结果"""
    print(f"\n{'='*60}")
    print(f"PEAD 策略回测结果")
    print(f"{'='*60}")
    
    for k, v in result.items():
        if k.startswith('pead_') and k.endswith('_trades'):
            continue
        if isinstance(v, float):
            if k in ('win_rate',):
                print(f"  {k:<18} {v:>8.1%}")
            elif 'return' in k or 'ret' in k:
                print(f"  {k:<18} {v:>+8.2%}")
            else:
                print(f"  {k:<18} {v:>8.2f}")
        else:
            print(f"  {k:<18} {v}")
    
    # 按退出原因
    print(f"\n{'='*60}")
    print(f"退出原因分布")
    print(f"{'='*60}")
    
    trades_df = pd.read_parquet(OUTPUT / "trades.parquet")
    for reason, grp in trades_df.groupby('exit_reason'):
        print(f"  {reason:<15} {len(grp):>4}笔  均收益 {grp['return'].mean():+7.2%}")


if __name__ == '__main__':
    print("加载 PEAD 策略...")
    pead = load_pead_strategy()
    print(f"  深幅下修事件: {len(pead.events)}")
    print(f"  时间范围: {min(e.event_date for e in pead.events).date()} ~ {max(e.event_date for e in pead.events).date()}")
    
    print(f"\n运行回测...")
    portfolio = Portfolio(initial_capital=1_000_000)
    portfolio.backtest([pead])
    
    result = portfolio.summary()
    
    # 保存
    eq_df = pd.DataFrame(portfolio.equity)
    eq_df.to_parquet(OUTPUT / "equity_curve.parquet", index=False)
    
    trades = pd.DataFrame(portfolio.trades) if portfolio.trades else pd.DataFrame()
    if not trades.empty:
        trades.to_parquet(OUTPUT / "trades.parquet", index=False)
    
    print_results(result)
    print(f"\n📁 结果保存到 {OUTPUT}")
