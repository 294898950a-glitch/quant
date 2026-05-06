"""
统一组合回测引擎
支持多策略并行：PEAD + 强赎窗口套利
"""
from __future__ import annotations
import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Protocol
from datetime import timedelta

# ============================================================
# Strategy Protocol
# ============================================================
@dataclass
class StrategyEvent:
    """统一的策略事件"""
    bond_id: str
    name: str
    event_date: pd.Timestamp       # 事件日 (T+0)
    strategy: str                  # 'pead' | 'redemption'
    entry_price: Optional[float] = None   # 开仓价 (T+0 close)
    hold_days: int = 60
    take_profit: Optional[float] = None   # 止盈 (%)
    stop_loss: Optional[float] = None     # 止损 (%)
    extra: Dict = field(default_factory=dict)

@dataclass 
class StrategyConfig:
    name: str
    events: List[StrategyEvent]
    price_lookup: Dict  # {(bond_id, event_date): {offset: price}}

# ============================================================
# Portfolio Engine
# ============================================================
@dataclass
class Position:
    bond_id: str
    name: str
    strategy: str
    entry_date: pd.Timestamp
    entry_price: float
    hold_days: int
    take_profit: Optional[float]
    stop_loss: Optional[float]
    exit_date: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None

class Portfolio:
    def __init__(self, initial_capital: float = 1_000_000):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: Dict[str, Position] = {}
        self.equity: List[Dict] = []
        self.trades: List[Dict] = []
    
    def backtest(self, strategies: List[StrategyConfig]):
        """运行多策略回测"""
        # 合并所有事件，构建交易日历
        all_events = []
        for cfg in strategies:
            for evt in cfg.events:
                all_events.append((cfg, evt))
        all_events.sort(key=lambda x: x[1].event_date)
        
        if not all_events:
            return
        
        # 交易日历
        first_date = min(e[1].event_date for e in all_events)
        last_date = max(e[1].event_date + timedelta(days=e[1].hold_days) for e in all_events)
        calendar = pd.date_range(first_date, last_date, freq='B')  # 仅交易日
        
        event_idx = 0
        for date in calendar:
            # === 持仓退出检查 ===
            self._check_exits(strategies, date)
            
            # === 新事件开仓 ===
            while event_idx < len(all_events):
                cfg, evt = all_events[event_idx]
                if evt.event_date > date:
                    break
                if evt.event_date == date:
                    self._open_position(cfg, evt)
                event_idx += 1
            
            # === 记录权益 ===
            equity_value = self._calc_equity(strategies, date)
            self.equity.append({
                'date': date,
                'equity': equity_value,
                'n_positions': len([p for p in self.positions.values() if p.exit_date is None]),
            })
    
    def _check_exits(self, strategies: List[StrategyConfig], date: pd.Timestamp):
        """检查持仓退出条件"""
        # 构建价格查找合并
        price_lookups = {s.name: s.price_lookup for s in strategies}
        
        exited = []
        for bid, pos in self.positions.items():
            if pos.exit_date is not None:
                continue
            
            days_held = (date - pos.entry_date).days
            lookup = price_lookups.get(pos.strategy, {})
            key = (bid, pos.entry_date)
            prices = lookup.get(key, {})
            
            if days_held not in prices:
                continue
            
            current_price = prices[days_held]
            ret = current_price / pos.entry_price - 1
            
            reason = None
            exit_price = None
            
            # 止盈
            if pos.take_profit and ret >= pos.take_profit:
                reason = 'take_profit'
                exit_price = current_price
            # 止损
            elif pos.stop_loss and ret <= pos.stop_loss:
                reason = 'stop_loss'
                exit_price = current_price
            # 到期
            elif days_held >= pos.hold_days:
                reason = 'time_exit'
                exit_price = current_price
            
            if reason:
                pos.exit_date = date
                pos.exit_price = exit_price
                pos.exit_reason = reason
                self.trades.append({
                    'bond_id': bid, 'name': pos.name, 'strategy': pos.strategy,
                    'entry_date': pos.entry_date, 'exit_date': date,
                    'entry_price': pos.entry_price, 'exit_price': exit_price,
                    'days_held': days_held, 'return': ret,
                    'exit_reason': reason,
                })
                exited.append(bid)
        
        for bid in exited:
            del self.positions[bid]
    
    def _open_position(self, cfg: StrategyConfig, evt: StrategyEvent):
        """开仓"""
        key = (evt.bond_id, evt.event_date)
        prices = cfg.price_lookup.get(key, {})
        if 0 not in prices:
            return
        
        self.positions[evt.bond_id] = Position(
            bond_id=evt.bond_id,
            name=evt.name,
            strategy=cfg.name,
            entry_date=evt.event_date,
            entry_price=prices[0],
            hold_days=evt.hold_days,
            take_profit=evt.take_profit,
            stop_loss=evt.stop_loss,
        )
    
    def _calc_equity(self, strategies, date) -> float:
        """计算当日权益 = 现金 + 持仓市值"""
        active = [p for p in self.positions.values() if p.exit_date is None]
        price_lookups = {s.name: s.price_lookup for s in strategies}
        
        position_value = 0.0
        for pos in active:
            lookup = price_lookups.get(pos.strategy, {})
            key = (pos.bond_id, pos.entry_date)
            prices = lookup.get(key, {})
            days_held = (date - pos.entry_date).days
            
            if days_held in prices and pos.entry_price > 0:
                units = self.cash / len(active) / pos.entry_price
                position_value += units * prices[days_held]
        
        # 未使用现金
        unused = self.cash - (self.cash * len(active) / max(len(active), 1))
        return unused + position_value if active else self.cash
    
    def summary(self) -> Dict:
        """策略绩效汇总"""
        if not self.equity:
            return {}
        
        eq = pd.DataFrame(self.equity)
        eq['daily_return'] = eq['equity'].pct_change()
        returns = eq['daily_return'].dropna()
        
        total_ret = eq['equity'].iloc[-1] / self.initial_capital - 1
        ann_ret = (1 + total_ret) ** (252 / max(len(returns), 1)) - 1
        vol = returns.std() * np.sqrt(252) if len(returns) > 1 else 0
        sharpe = (returns.mean() / returns.std() * np.sqrt(252)) if vol > 0 else 0
        max_dd = (eq['equity'] / eq['equity'].cummax() - 1).min()
        
        trades = pd.DataFrame(self.trades) if self.trades else pd.DataFrame()
        
        result = {
            'total_return': total_ret,
            'annualized': ann_ret,
            'sharpe': sharpe,
            'volatility': vol,
            'max_drawdown': max_dd,
            'n_trades': len(trades),
        }
        
        if not trades.empty:
            result['win_rate'] = (trades['return'] > 0).mean()
            result['avg_return'] = trades['return'].mean()
            result['avg_win'] = trades[trades['return'] > 0]['return'].mean()
            result['avg_loss'] = trades[trades['return'] < 0]['return'].mean()
            
            # 按策略拆分
            for strat_name in trades['strategy'].unique():
                st = trades[trades['strategy'] == strat_name]
                result[f'{strat_name}_trades'] = len(st)
                result[f'{strat_name}_avg_ret'] = st['return'].mean()
                result[f'{strat_name}_win_rate'] = (st['return'] > 0).mean()
        
        return result
