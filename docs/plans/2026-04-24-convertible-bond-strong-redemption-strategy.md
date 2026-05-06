# 可转债强赎博弈策略 — 实施计划

> **For Hermes:** 使用 subagent-driven-development 技能逐步实施。

**目标：** 搭建可转债强赎博弈的公告监控 + 数据采集 + 信号生成管道，从零到可回测。

**架构：**
```
┌─────────────────────┐    ┌──────────────────┐    ┌───────────────────┐
│  Layer 1: 公告监控    │ →  │ Layer 2: 数据聚合  │ →  │ Layer 3: 信号引擎  │
│  ─ 巨潮资讯轮询       │    │  ─ 转债行情数据    │    │  ─ Logit预测模型   │
│  ─ 关键词过滤         │    │  ─ 正股数据       │    │  ─ 信号分级Trigger │
│  ─ Telegram推送       │    │  ─ 舆情情绪指标    │    │  ─ 策略回测框架    │
└─────────────────────┘    └──────────────────┘    └───────────────────┘
```

**Tech Stack:** Python 3.12 + akshare + pandas/numpy + requests + BeautifulSoup + schedule (定时) + python-telegram-bot (推送)

**项目位置:** `~/projects/quant/` (复用现有 quant 项目结构)
**新模块目录:** `~/projects/quant/strategies/cb_redemption/`

---

## 任务导航

### Phase 1: 基础设施 (Task 1-3)
Task 1: 安装依赖 + 创建模块骨架
Task 2: 可转债基础数据层 (行情/正股)
Task 3: 巨潮资讯公告采集器

### Phase 2: 信号核心 (Task 4-5)
Task 4: 强赎状态判断 + 进度追踪
Task 5: Logit 信号生成器

### Phase 3: 交付 (Task 6-7)
Task 6: 定时监控 + Telegram 推送
Task 7: 端到端测试 + Notion Wiki 入库

---

### Task 1: 安装依赖 + 创建模块骨架

**Objective:** 安装所有必要 Python 库，创建可转债策略模块的目录和空文件骨架。

**Files:**
- Create: `~/projects/quant/strategies/__init__.py`
- Create: `~/projects/quant/strategies/cb_redemption/__init__.py`
- Create: `~/projects/quant/strategies/cb_redemption/config.py`
- Create: `~/projects/quant/strategies/cb_redemption/data.py`
- Create: `~/projects/quant/strategies/cb_redemption/monitor.py`
- Create: `~/projects/quant/strategies/cb_redemption/signals.py`
- Create: `~/projects/quant/strategies/cb_redemption/push.py`
- Create: `~/projects/quant/strategies/cb_redemption/main.py`

**Step 1: 安装依赖**
```bash
cd ~/projects/quant
pip install akshare feedparser beautifulsoup4 schedule python-telegram-bot
```

**Step 2: 创建目录结构**
```bash
mkdir -p ~/projects/quant/strategies/cb_redemption
touch ~/projects/quant/strategies/__init__.py
touch ~/projects/quant/strategies/cb_redemption/__init__.py
```

**Step 3: 写 config.py**

```python
"""
可转债强赎博弈策略 — 配置文件
"""
import os
from pathlib import Path

# 项目根目录
ROOT_DIR = Path(__file__).resolve().parent.parent.parent

# Telegram Bot 配置
TELEGRAM_BOT_TOKEN = os.getenv("HERMES_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = "6403706808"  # Jay

# 数据目录
DATA_DIR = ROOT_DIR / "data" / "cb_redemption"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 监控配置
MONITOR_INTERVAL_MINUTES = 15       # 公告轮询间隔
MARKET_OPEN_HOUR = 9                # 开盘小时
MARKET_CLOSE_HOUR = 15              # 收盘小时
TRADE_DAYS_ONLY = True              # 仅交易日运行

# 巨潮资讯配置
CNINFO_BASE_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_ORG_ID = "gssz0003001"       # 深交所全市场公告（可转债相关）

# 强赎信号参数
REDEMPTION_THRESHOLD_DAYS = 15      # 距触发日 ≤15天触发预警
PREMIUM_RATIO_WARN = 30             # 转股溢价率 >30% 警告
UNDERLYING_MOMENTUM_DAYS = 5        # 正股动量窗口（日）
```

**Step 4: 写 data.py 骨架 + signals.py 骨架 + monitor.py 骨架 + push.py 骨架 + main.py 骨架**

每个文件写一个空类和 main 函数，确保 import 不报错。

**Step 5: 验证**
```bash
cd ~/projects/quant && python3 -c "from strategies.cb_redemption import config; print('config OK')"
cd ~/projects/quant && python3 -c "from strategies.cb_redemption.data import *; print('data OK')"
```

---

### Task 2: 可转债基础数据层 (行情/正股)

**Objective:** 实现 `data.py` — 通过 akshare 获取可转债列表、实时行情、历史数据和对应的正股数据。

**Files:**
- Modify: `~/projects/quant/strategies/cb_redemption/data.py`

**核心函数列表:**

```python
async def get_cb_list() -> pd.DataFrame:
    """获取全市场可转债列表（含代码、名称、剩余规模、到期日等）"""

async def get_cb_quote(cb_code: str) -> dict:
    """获取单只可转债实时行情（现价、涨跌幅、成交量、转股溢价率等）"""

async def get_cb_daily(cb_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取可转债日线数据"""

async def get_stock_daily(stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取正股日线数据"""

def is_trade_day() -> bool:
    """判断今天是否为交易日（通过检查是否有实时行情）"""
```

使用 akshare 的数据接口：
- `bond_zh_cov()` — 全市场可转债列表
- `bond_cov_jsl()` — 集思录数据（含转股溢价率、双低等）
- `stock_zh_a_daily()` — 获取日线
- `tool_trade_date_hist_sina()` — 交易日历

---

### Task 3: 巨潮资讯公告采集器

**Objective:** 实现 `monitor.py` — 轮询巨潮资讯，通过关键词过滤获取可转债相关的下修/强赎/回售公告。

**Files:**
- Modify: `~/projects/quant/strategies/cb_redemption/monitor.py`

**核心函数:**

```python
async def fetch_cninfo_announcements(keywords: list[str], page_size: int = 20) -> list[dict]:
    """
    从巨潮资讯查询公告。
    URL: http://www.cninfo.com.cn/new/hisAnnouncement/query
    POST 参数: orgId, category, pageNum, pageSize, seDate, stock
    关键词过滤: 下修, 强赎, 回售, 不赎回, 提前赎回
    """

async def parse_announcement(ann: dict) -> dict:
    """解析单条公告，提取：标题、公告日期、PDF链接、摘要（title截取）"""

def classify_announcement(title: str) -> str:
    """分类公告类型: redemption（强赎）, revision（下修）, putback（回售）, other"""
```

**公告 API 细节：**
- 端点: `http://www.cninfo.com.cn/new/hisAnnouncement/query`
- Headers: `User-Agent: Mozilla/5.0`, `Content-Type: application/x-www-form-urlencoded; charset=utf-8`
- 需要管理 `Cookie` 会话（先 GET 首页获取 cookie）
- 分类关键词表：
  - 强赎: `['赎回', '强赎', '提前赎回', '不赎回', '不行使赎回权']`
  - 下修: `['下修', '向下修正', '转股价格']`
  - 回售: `['回售', '有条件回售']`

---

### Task 4: 强赎状态判断 + 进度追踪

**Objective:** 对每只可转债，计算其强赎进度（触发天数、距触发日数、溢价率）并生成状态标签。

**Files:**
- Modify: `~/projects/quant/strategies/cb_redemption/signals.py`

**核心逻辑:**

```python
class RedemptionTracker:
    """
    强赎状态追踪器

    强赎触发条件（通常）：
    - 正股价格 ≥ 转股价 × 130% 持续 30 个交易日中的 15 个交易日
    - 或者余额 < 3000 万元

    输入：转债代码
    输出：状态字典
    """

    async def compute_status(self, cb_code: str) -> dict:
        """
        返回:
        {
            "cb_code": "123456",
            "cb_name": "XX转债",
            "is_triggered": bool,         # 是否已触发强赎条件
            "trigger_progress": 0.67,      # 触发天数/15
            "days_remaining": 5,           # 还差几天到截止日（如果已触发则为0）
            "conversion_premium": 3.5,     # 转股溢价率 %
            "stock_price_above_130pct": bool,  # 正股当前是否在130%转股价以上
            "volume_anomaly": bool,        # 成交量异常
            "status_label": "watching|approaching|triggered|active_redeeming|done"
        }
        """
```

**status_label 含义:**
- `watching`: 正股低于 130% 转股价，距离触发还远
- `approaching`: 正股已满足 130% 条件，正在凑天数（进度 > 0）
- `triggered`: 已满足触发条件，但公司尚未公告是否行使赎回权
- `active_redeeming`: 公司已公告行使赎回权，转债处于赎回期
- `done`: 强赎已完成（已退市或转为股票）

---

### Task 5: Logit 信号生成器

**Objective:** 基于收集到的数据因子，运行一个简单 Logit 模型，输出强赎概率预测值 + 信号等级。

**Files:**
- Modify: `~/projects/quant/strategies/cb_redemption/signals.py`

**Logit 模型公式:**
```
P(redemption) = 1 / (1 + exp(-(β0 + β1*x1 + β2*x2 + ...)))
```

**因子（x1..x5）:**
1. **转股溢价率** (`conversion_premium`) — 负相关：溢价越低越可能强赎
2. **触发进度** (`trigger_progress`) — 正相关：天数越接近15越可能
3. **正股动量** (`stock_momentum_5d`) — 正相关：正股近期上涨更易触发
4. **余额对数** (`log_balance`) — 负相关：余额越小越可能强赎
5. **是否已触发** (`is_triggered`, 0/1) — 强正相关

**初始系数（基于文献和常识的 expert prior）:**
```
β0 = -4.0
β1 (溢价率) = -0.08
β2 (触发进度) = 4.0
β3 (正股动量) = 0.5
β4 (余额对数) = -0.3
β5 (已触发) = 3.0
```

**信号等级:**
| 概率区间 | 信号 | 含义 |
|----------|------|------|
| < 10% | 🟢 观望 | 强赎概率极低 |
| 10-30% | 🟡 关注 | 进入关注列表 |
| 30-60% | 🟠 预警 | 强赎风险上升 |
| 60-85% | 🔴 警惕 | 随时可能公告 |
| > 85% | 🚨 行动 | 高概率即将公告强赎 |

---

### Task 6: 定时监控 + Telegram 推送

**Objective:** 组装完整 pipeline → 定时运行 → 推送结果到 Telegram。

**Files:**
- Modify: `~/projects/quant/strategies/cb_redemption/main.py`
- Modify: `~/projects/quant/strategies/cb_redemption/push.py`

**main.py 工作流:**
```python
async def run_pipeline():
    """
    1. data.get_cb_list() → 全市场转债列表
    2. 对每只转债 data.get_cb_quote() + data.get_stock_daily()
    3. signals.RedemptionTracker.compute_status() → 计算强赎状态
    4. signals.LogitScorer.score() → 计算强赎概率
    5. 过滤出信号等级 ≥ 🟠 预警 的转债
    6. push.send_telegram_alert() → 推送
    """
```

**push.py 推送格式:**
```
🔥 可转债强赎监控 | YYYY-MM-DD HH:MM

🚨 行动级 (≥85%)
📌 XX转债(123456) | 概率 92% | 溢价率 1.2% | 进度 15/15 | 已公告

🔴 警惕级 (60-85%)
📌 YY转债(123789) | 概率 78% | 溢价率 3.5% | 进度 12/15

🟠 预警级 (30-60%)
📌 ZZ转债(123012) | 概率 45% | 溢价率 8.2% | 进度 8/15
```

---

### Task 7: 端到端测试 + Notion Wiki 入库

**Objective:** 运行完整 pipeline 验证功能，将策略研究成果入库 llmwiki。

**Step 1: 运行端到端测试**
```bash
cd ~/projects/quant
python3 -m strategies.cb_redemption.main --dry-run
```

**Step 2: 入库 Notion Wiki (Raw Inbox) — 使用 llmwiki-raw-ingest 技能**

将本策略的实施总结和运行结果作为 Raw Inbox 条目入库，触发编译。

**Step 3: 手动验证 Telegram 推送**
确认在 Telegram 上收到了监控推送消息。

---

## 执行顺序

1. **Task 1** → 基础设施（必须最先，不然什么都没有）
2. **Task 2** → 数据层（a股可转债行情数据）
3. **Task 3** → 公告监控层（巨潮资讯）
4. **Task 4** → 强赎进度计算（信号逻辑核心）
5. **Task 5** → Logit 评分（信号引擎）
6. **Task 6** → 推送 + 定时运行（交付件）
7. **Task 7** → 测试 + 入库

每个任务完成后的验证步骤都在任务描述中。
当所有任务完成，你将得到一个定时运行的 Telegram 监控 bot。
