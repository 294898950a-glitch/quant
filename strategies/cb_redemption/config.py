"""
可转债强赎博弈策略 — 配置中心

所有可配置参数集中管理，环境变量优先。
"""
import os
from pathlib import Path

# === 项目路径 ===
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT_DIR / "data" / "cb_redemption"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR = ROOT_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# === Telegram Bot（Hermes 原生推送）===
def _resolve_bot_token() -> str:
    """从环境变量或 Hermes 配置中读取 Bot Token。"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("HERMES_TELEGRAM_BOT_TOKEN")
    if token:
        return token
    # 尝试从 Hermes .env 中读取
    hermes_env = Path.home() / ".hermes" / ".env"
    if hermes_env.exists():
        for line in hermes_env.read_text().splitlines():
            line = line.strip()
            # 支持 TELEGRAM_BOT_TOKEN 或 HERMES_TELEGRAM_BOT_TOKEN
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip("\"'")
                return token
            if line.startswith("HERMES_TELEGRAM_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip("\"'")
                return token
    return ""

TELEGRAM_BOT_TOKEN = _resolve_bot_token()
TELEGRAM_CHAT_ID = "6403706808"

# === 监控时间 ===
MONITOR_INTERVAL_MINUTES = 30         # 轮询间隔（非交易时段不运行）
MARKET_OPEN_HOUR = 9                  # 开盘小时
MARKET_CLOSE_HOUR = 15                # 收盘小时
TRADE_DAYS_ONLY = True                # 仅交易日运行

# === 巨潮资讯公告采集 ===
CNINFO_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
}
# 公告分类关键词
KEYWORD_MAP = {
    "redemption": ["赎回", "强赎", "提前赎回", "不赎回", "不行使赎回权"],
    "revision": ["下修", "向下修正", "转股价格修正"],
    "putback": ["回售", "有条件回售"],
}

# === 强赎信号参数 ===
REDEMPTION_THRESHOLD_DAYS = 15        # 强赎触发需要的最少交易日
REDEMPTION_PRICE_RATIO = 130          # 正股价 / 转股价 >= 130%
TRIGGER_WINDOW_DAYS = 30              # 触发窗口总天数
PREMIUM_RATIO_WARN = 30               # 转股溢价率 >30% 进入预警
STOCK_MOMENTUM_WINDOW = 5             # 正股动量计算窗口（日）
VOLUME_ANOMALY_RATIO = 2.0            # 成交量 / 20日均量 > 2.0 视为异常

# === Logit 模型权重（供 optimizer 读写）===
LOGIT_WEIGHTS = [2.1997, -0.6910, -3.6818, 1.9357, -0.6313, -0.5494, -0.1504, -0.4190]
LOGIT_WEIGHT_NAMES = ["redeem_progress", "premium_ratio", "remaining_size", "stock_momentum", "market_sentiment",
                      "ai_signal_score", "ai_reduction_score", "ai_is_original"]

# === 阈值（供 optimizer 读写）===
DEFAULT_THRESHOLDS_CONFIG = {
    "action": 0.65,
    "alert": 0.45,
    "watch": 0.25,
}

# === 短持模式回测参数 ===
SHORT_HOLD_PARAMS = {
    "hold_max_days": 5,
    "target_exit_pct": 4.0,
    "stop_loss_pct": -3.0,
    "max_positions": 10,
    "top_k": 10,
}

# === 优化器基线持久化文件 ===
OPTIMIZER_BASELINE_FILE = str(DATA_DIR / "optimizer_baseline.json")

# === CMA-ES 进化状态持久化（跨轮次延续搜索）===
# 每次优化结束后将 CMAEvolutionStrategy 的完整状态
# （协方差矩阵C、步长σ、进化路径pc、均值等）pickle 到磁盘。
# 下轮启动时恢复状态，实现真正的持续优化。
CMA_ES_STATE_FILE = str(DATA_DIR / "cma_es_state.pkl")
# 当基线权重发生变化时（因发现了更优参数而更新），自动重置状态。
# 这个计数器确保状态和基线保持同步。
CMA_ES_STATE_VERSION = 0

# === 文本数据采集 ===
# 东方财富搜索 API
EASTMONEY_SEARCH_URL = "http://search-api-web.eastmoney.com/search/jsonp"
# 可转债相关搜索关键词
CB_NEWS_KEYWORDS = [
    "可转债", "可转债强赎", "可转债下修", "可转债回售",
    "转债赎回", "转债下修", "转债强赎",
]
# 新浪财经滚动新闻
SINA_ROLL_URL = "https://roll.finance.sina.com.cn/api/news_list.php"
SINA_ZB_URL = "https://zhibo.sina.com.cn/api/zhibo/feed"
# 证券时报 RSS
STCN_RSS_URL = "https://app.stcn.com/rss.php"
STCN_RSS_CATIDS = {29: "快讯", 17: "股票", 340: "滚动新闻", 41: "股票情报"}
# 采集控制
NEWS_MAX_PAGES = 3             # 每源翻页数
NEWS_REQUEST_SLEEP = 0.5       # 请求间隔（秒），新浪建议 0.8+
NEWS_OUTPUT_FILE = str(DATA_DIR / "cb_news.parquet")

