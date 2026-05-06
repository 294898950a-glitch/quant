"""
PEAD 策略配置
"""
from pathlib import Path

# 数据路径
DATA_DIR = Path.home() / "projects/quant/data/cb_pead"
EVENTS_CSV = DATA_DIR / "raw/cb_down_events_with_returns.csv"
SERIES_CSV = DATA_DIR / "raw/cb_pead_series.csv"
OUTPUT_DIR = DATA_DIR / "backtest"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 策略参数
RATIO_THRESHOLD = 0.75        # 大幅下修阈值
HOLD_DAYS = 60                # 持有天数
MAX_POSITIONS = 20            # 最大同时持仓数
POSITION_WEIGHT = "equal"     # 等权

# 交易成本
COMMISSION = 0.0003           # 佣金 0.03%
STAMP_DUTY = 0.001            # 印花税 0.1% (CB 不交，但保留)
SLIPPAGE = 0.001              # 滑点 0.1%
TOTAL_TC = COMMISSION + SLIPPAGE  # 总交易成本

# 风控
STOP_LOSS = -0.10             # 单笔止损 -10%
TAKE_PROFIT = None            # 止盈 (None=不限)

# 基准
BENCHMARK = "cb_equal_weight"  # 转债等权指数
