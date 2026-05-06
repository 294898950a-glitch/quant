"""
可转债强赎信号模块 —— 状态追踪 + Logit 概率评分。

包含两个核心类：
- RedemptionTracker：计算每只转债的强赎进度和状态标签
- LogitScorer：基于 5 个因子 + expert prior 系数，输出强赎概率与信号等级

依赖：
- data.py（get_cb_redeem_data, get_stock_daily）
- config.py（STOCK_MOMENTUM_WINDOW, VOLUME_ANOMALY_RATIO 等）
"""

from __future__ import annotations

import logging
import math
import os
import pickle
import time
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, date
from typing import Any

import pandas as pd

from strategies.cb_redemption.config import (
    STOCK_MOMENTUM_WINDOW,
    VOLUME_ANOMALY_RATIO,
    REDEMPTION_THRESHOLD_DAYS,
    TRIGGER_WINDOW_DAYS,
)
from strategies.cb_redemption.data import get_cb_redeem_data, get_stock_daily

logger = logging.getLogger(__name__)

# =============================================================================
# RedemptionTracker —— 强赎状态追踪器
# =============================================================================


class RedemptionTracker:
    """
    强赎状态追踪器。

    强赎触发条件（通常）：
    - 正股价格 ≥ 转股价 × 130% 持续 30 个交易日中的 15 个交易日
    - 或者余额 < 3000 万元

    输入：转债代码
    输出：状态字典
    """

    def __init__(self) -> None:
        # 可缓存最近一次的 redeem_data，避免重复请求
        self._redeem_cache: pd.DataFrame | None = None
        self._cache_time: datetime | None = None
        # 正股历史数据缓存 {stock_code: pd.DataFrame}
        self._stock_cache: dict[str, pd.DataFrame] = {}
        self._stock_cache_time: float = 0.0

    def preload_stock_cache(self, cb_codes: list[str] | None = None, max_workers: int = 20) -> int:
        """
        预加载所有正股历史数据到缓存，避免逐只请求。

        使用 ThreadPoolExecutor 并发拉取 + pickle 本地文件缓存，
        可将 340 个串行请求（~340s）压到 ~15-20s。

        Parameters
        ----------
        cb_codes : list[str] | None
            需预加载的转债代码列表。为 None 时从全市场数据提取。
        max_workers : int
            并发线程数，默认 20。

        Returns
        -------
        int
            缓存的正股数量
        """
        from strategies.cb_redemption.config import DATA_DIR

        df = self._get_redeem_data()
        if df.empty or len(df) == 0:
            return 0

        if cb_codes is not None and len(cb_codes) > 0:
            codes_col = None
            for col in ["代码", "债券代码", "symbol"]:
                if col in df.columns:
                    codes_col = col
                    break
            if codes_col:
                df = df[df[codes_col].astype(str).isin(cb_codes)]

        stock_codes = set()
        for col in ["正股代码", "stock_code", "正股"]:
            if col in df.columns:
                for v in df[col].dropna():
                    s = str(v).strip()
                    if s and s != "nan":
                        stock_codes.add(s)
                if stock_codes:
                    break

        if not stock_codes:
            return 0

        logger.info(f"📥 预加载 {len(stock_codes)} 只正股 (并发 {max_workers} 线程)...")
        end = date.today()
        start = end - timedelta(days=60)
        cache_dir = DATA_DIR / "stock_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        today_key = end.isoformat()

        # 统一规范化
        normalized_map = {}
        for sc in sorted(stock_codes):
            try:
                normalized_map[sc] = self._normalize_stock_code(sc)
            except Exception:
                pass

        already_cached = 0
        to_fetch = {}

        for sc, normalized in normalized_map.items():
            # 内存缓存命中
            if normalized in self._stock_cache:
                already_cached += 1
                continue
            # 文件缓存命中（今日数据）
            cache_path = cache_dir / f"{normalized}_{today_key}.pkl"
            if cache_path.exists():
                try:
                    with open(cache_path, "rb") as f:
                        self._stock_cache[normalized] = pickle.load(f)
                    already_cached += 1
                    continue
                except Exception:
                    pass
            to_fetch[sc] = normalized

        if not to_fetch:
            logger.info(f"✅ 全部 {len(stock_codes)} 只已在缓存中")
            return already_cached

        # 并发拉取
        loaded = 0
        errors = 0
        start_time = time.time()

        def fetch_one(sc: str, normalized: str) -> tuple[str, pd.DataFrame | None]:
            try:
                df_stock = get_stock_daily(
                    normalized,
                    start_date=start.isoformat(),
                    end_date=end.isoformat(),
                )
                if not df_stock.empty:
                    return normalized, df_stock
                return normalized, None
            except Exception as e:
                logger.warning(f"预加载 {sc}({normalized}) 失败: {e}")
                return normalized, None

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(fetch_one, sc, n): (sc, n)
                for sc, n in to_fetch.items()
            }
            for future in as_completed(futures):
                normalized, df_stock = future.result()
                if df_stock is not None:
                    self._stock_cache[normalized] = df_stock
                    # 写入文件缓存
                    cache_path = cache_dir / f"{normalized}_{today_key}.pkl"
                    try:
                        with open(cache_path, "wb") as f:
                            pickle.dump(df_stock, f)
                    except Exception:
                        pass
                    loaded += 1
                else:
                    errors += 1

        elapsed = time.time() - start_time
        self._stock_cache_time = datetime.now().timestamp()
        logger.info(
            f"✅ 正股预加载: 新载 {loaded} 只, 缓存命中 {already_cached} 只, "
            f"失败 {errors} 只, 耗时 {elapsed:.1f}s "
            f"(缓存共 {len(self._stock_cache)} 只)"
        )
        return loaded

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    def compute_status(self, cb_code: str) -> dict[str, Any]:
        """
        对单只可转债（cb_code）计算强赎进度和状态标签。

        Parameters
        ----------
        cb_code : str
            可转债代码，例如 '123456'。

        Returns
        -------
        dict
            {
                "cb_code": "123456",
                "cb_name": "XX转债",
                "is_triggered": bool,              # 是否已触发强赎条件
                "trigger_progress": 0.67,           # 触发天数/15（0~1）
                "days_remaining": 5,                # 距截止日还差几天（已触发则为0）
                "conversion_premium": 3.5,          # 转股溢价率（百分数）
                "stock_price_above_130pct": bool,   # 正股当前是否 >= 130% 转股价
                "volume_anomaly": bool,             # 成交量是否异常
                "status_label": str,                # 状态标签
                "cb_price": 0.0,                    # 转债当前价格
                "stock_code": "",                   # 正股代码
                "stock_name": "",                   # 正股名称
                "stock_price": 0.0,                 # 正股当前价格
                "conversion_price": 0.0,            # 转股价
                "trigger_price": 0.0,               # 强赎触发价
                "redemption_price": 0.0,            # 强赎价
                "remaining_balance": 0.0,           # 剩余规模（亿元）
                "trigger_days_count": 0,            # 已触发天数
            }
        """
        # 1. 获取全市场强赎数据
        df = self._get_redeem_data()
        if df.empty:
            logger.warning("RedemptionTracker: 强赎数据为空, cb_code=%s", cb_code)
            return self._empty_result(cb_code)

        # 2. 按代码过滤（支持精确匹配和模糊匹配）
        row = self._find_row(df, cb_code)
        if row is None:
            logger.warning("RedemptionTracker: 未找到转债 %s", cb_code)
            return self._empty_result(cb_code)

        # 3. 提取基础字段
        result = self._extract_basic_fields(row)
        if result["conversion_price"] <= 0:
            logger.warning("RedemptionTracker: %s 转股价无效", cb_code)
            return self._empty_result(cb_code)

        # 4. 计算触发进度
        trigger_progress, trigger_days_count = self._compute_trigger_progress(row)
        is_triggered = trigger_days_count >= REDEMPTION_THRESHOLD_DAYS

        # 5. 获取正股历史数据计算动量 & 成交量异常
        stock_code = result["stock_code"]
        # 优先使用预加载的 stock_cache
        normalized_sc = self._normalize_stock_code(stock_code) if stock_code else ""
        if normalized_sc and normalized_sc in self._stock_cache:
            df_stock = self._stock_cache[normalized_sc]
            momentum_5d, volume_anomaly = self._calc_stock_momentum_from_df(df_stock)
        else:
            momentum_5d, volume_anomaly = self._compute_stock_signals(stock_code)

        # 6. 计算溢价率（如果 field 缺失则自行计算）
        conversion_premium = result["conversion_premium"]
        if conversion_premium is None or math.isnan(conversion_premium):
            conversion_premium = self._calc_conversion_premium(
                result["cb_price"],
                result["stock_price"],
                result["conversion_price"],
            )

        # 7. 判断正股是否在 130% 转股价之上
        stock_price_above_130pct = (
            result["stock_price"] >= result["conversion_price"] * 1.30
            if result["stock_price"] > 0
            else False
        )

        # 8. 从原数据中提取强赎状态（中文，如 "已公告强赎"、"已触发"）
        redeem_status_cn = ""
        for col in ["强赎状态", "redeem_status", "status"]:
            if col in row.index and pd.notna(row[col]):
                redeem_status_cn = str(row[col]).strip()
                break

        # 9. 状态标签
        status_label = self._classify_status(
            is_triggered=is_triggered,
            trigger_progress=trigger_progress,
            stock_price_above_130pct=stock_price_above_130pct,
            trigger_days_count=trigger_days_count,
            redeem_status_cn=redeem_status_cn,
        )

        # 9. 组装输出
        result.update(
            {
                "is_triggered": is_triggered,
                "trigger_progress": round(trigger_progress, 4),
                "trigger_days_count": trigger_days_count,
                "days_remaining": max(0, REDEMPTION_THRESHOLD_DAYS - trigger_days_count),
                "conversion_premium": round(conversion_premium, 2) if conversion_premium is not None else 999.0,
                "stock_price_above_130pct": stock_price_above_130pct,
                "volume_anomaly": volume_anomaly,
                "status_label": status_label,
                "stock_momentum_5d": round(momentum_5d, 4),
            }
        )
        return result

    def batch_compute(self, cb_codes: list[str] | None = None) -> list[dict[str, Any]]:
        """
        批量计算多只转债的状态。

        Parameters
        ----------
        cb_codes : list[str] | None
            转债代码列表，为 None 时对全市场计算。

        Returns
        -------
        list[dict]
            每只转债的状态字典列表。
        """
        df = self._get_redeem_data()
        if df.empty:
            return []

        if cb_codes is not None:
            codes_to_process = cb_codes
        else:
            codes_to_process = df["代码"].unique().tolist()

        results: list[dict[str, Any]] = []
        for code in codes_to_process:
            try:
                status = self.compute_status(code)
                results.append(status)
            except Exception as exc:
                logger.error("batch_compute: %s 处理失败: %s", code, exc)
                results.append(self._empty_result(code))
        return results

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _get_redeem_data(self) -> pd.DataFrame:
        """获取（可能缓存的）强赎数据。"""
        now = datetime.now()
        if self._redeem_cache is not None and self._cache_time is not None:
            if (now - self._cache_time) < timedelta(minutes=5):
                return self._redeem_cache

        df = get_cb_redeem_data()
        if not df.empty:
            self._redeem_cache = df
            self._cache_time = now
        return df

    def _find_row(self, df: pd.DataFrame, cb_code: str) -> pd.Series | None:
        """在 DataFrame 中查找对应转债的行。"""
        # 尝试精确匹配
        for col in ["代码", "债券代码", "symbol"]:
            if col in df.columns:
                mask = df[col].astype(str).str.strip() == cb_code.strip()
                if mask.any():
                    return df[mask].iloc[0]

        # 尝试模糊匹配（包含匹配）
        for col in ["代码", "债券代码", "symbol"]:
            if col in df.columns:
                mask = df[col].astype(str).str.contains(cb_code.strip())
                if mask.any():
                    return df[mask].iloc[0]

        return None

    def _extract_basic_fields(self, row: pd.Series) -> dict[str, Any]:
        """从行数据中提取基础字段，统一列名映射。"""
        # 列名映射（akshare 返回的中文列名 → 统一键名）
        field_map: dict[str, list[str]] = {
            "cb_code": ["代码", "债券代码", "symbol"],
            "cb_name": ["名称", "债券简称", "name"],
            "cb_price": ["现价", "trade", "price"],
            "stock_code": ["正股代码", "stock_code", "正股"],
            "stock_name": ["正股名称", "stock_name", "正股简称"],
            "stock_price": ["正股价", "stock_price"],
            "conversion_price": ["转股价", "conv_price"],
            "trigger_price": ["强赎触发价", "call_price", "trigger_price"],
            "redemption_price": ["强赎价", "redeem_price"],
            "remaining_balance": ["剩余规模", "剩余规模(亿)", "remain_size"],
            "trigger_days_count": ["强赎天计数", "trigger_days", "count"],
            "conversion_premium": ["转股溢价率", "溢价率", "premium_ratio"],
        }

        result: dict[str, Any] = {}
        for key, candidates in field_map.items():
            val = None
            for col in candidates:
                if col in row.index and pd.notna(row[col]):
                    val = row[col]
                    break
            # 尝试转数值（仅对数值型字段）
            string_fields = {"cb_code", "cb_name", "stock_code", "stock_name"}
            if val is not None:
                if key in string_fields:
                    val = str(val)
                else:
                    try:
                        val = float(val)
                    except (ValueError, TypeError):
                        val = str(val)
            else:
                val = 0.0 if key not in string_fields else ""
            result[key] = val

        return result

    def _compute_trigger_progress(
        self, row: pd.Series
    ) -> tuple[float, int]:
        """
        计算触发进度。

        Returns
        -------
        (progress, trigger_days_count)
            progress: 触发天数 / 15, 截断到 [0, 1]
            trigger_days_count: 已触发天数
        """
        trigger_days = 0
        # 尝试从 "强赎天计数" 字段读取，格式如 "11/15 | 30"
        for col in ["强赎天计数", "trigger_days", "count"]:
            if col in row.index and pd.notna(row[col]):
                raw = str(row[col]).strip()
                try:
                    # 格式 "11/15 | 30"
                    if "|" in raw:
                        left = raw.split("|")[0].strip()
                    else:
                        left = raw
                    if "/" in left:
                        count_str = left.split("/")[0].strip()
                        trigger_days = int(count_str)
                    else:
                        trigger_days = int(float(raw))
                except (ValueError, TypeError):
                    pass
                break

        # 如果强赎状态列包含 "已触发"，但天数为 0，则视为已触发满额
        status_val = ""
        for col in ["强赎状态", "redeem_status", "status"]:
            if col in row.index and pd.notna(row[col]):
                status_val = str(row[col])
                break

        if trigger_days == 0 and "已触发" in status_val:
            trigger_days = REDEMPTION_THRESHOLD_DAYS

        # 截断到阈值
        trigger_days = min(trigger_days, REDEMPTION_THRESHOLD_DAYS)
        progress = trigger_days / REDEMPTION_THRESHOLD_DAYS if REDEMPTION_THRESHOLD_DAYS > 0 else 0.0

        return min(progress, 1.0), trigger_days

    def _compute_stock_signals(
        self, stock_code: str
    ) -> tuple[float, bool]:
        """
        计算正股动量和成交量异常。

        Parameters
        ----------
        stock_code : str
            正股代码（可以带交易所前缀 'sh600036' 或不带 '603901'）。

        Returns
        -------
        (momentum_5d, volume_anomaly)
            momentum_5d: 近5日涨跌幅（%）
            volume_anomaly: 当日成交量是否异常
        """
        if not stock_code:
            return 0.0, False

        # 补全交易所前缀（akshare 要求 'sh600036' 格式）
        stock_code = self._normalize_stock_code(stock_code)

        end = date.today()
        # 取约 30 个交易日的数据，以便计算 20 日均量
        start = end - timedelta(days=60)

        df = get_stock_daily(
            stock_code,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )
        if df.empty or len(df) < STOCK_MOMENTUM_WINDOW + 1:
            return 0.0, False

        # 确保按日期升序
        df = df.sort_values("date").reset_index(drop=True)

        # 动量 = 最近 STOCK_MOMENTUM_WINDOW 日的累计涨跌幅
        if "close" in df.columns:
            recent_close = df["close"].iloc[-STOCK_MOMENTUM_WINDOW:].values
            if len(recent_close) >= 2 and recent_close[0] > 0:
                momentum_5d = (recent_close[-1] / recent_close[0] - 1) * 100
            else:
                momentum_5d = 0.0
        else:
            momentum_5d = 0.0

        # 成交量异常：当日成交量 / 20日均量 > VOLUME_ANOMALY_RATIO
        volume_anomaly = False
        if "volume" in df.columns and len(df) >= 20:
            latest_vol = df["volume"].iloc[-1]
            avg_vol_20 = df["volume"].iloc[-20:].mean()
            if avg_vol_20 > 0 and (latest_vol / avg_vol_20) > VOLUME_ANOMALY_RATIO:
                volume_anomaly = True

        return round(momentum_5d, 2), volume_anomaly

    @staticmethod
    def _calc_stock_momentum_from_df(df: pd.DataFrame) -> tuple[float, bool]:
        """
        从已缓存的 DataFrame 快速计算正股动量和成交量异常。

        Returns
        -------
        (momentum_5d, volume_anomaly)
        """
        if df.empty or len(df) < STOCK_MOMENTUM_WINDOW + 1:
            return 0.0, False

        df = df.sort_values("date").reset_index(drop=True)

        momentum_5d = 0.0
        if "close" in df.columns:
            recent_close = df["close"].iloc[-STOCK_MOMENTUM_WINDOW:].values
            if len(recent_close) >= 2 and recent_close[0] > 0:
                momentum_5d = (recent_close[-1] / recent_close[0] - 1) * 100

        volume_anomaly = False
        if "volume" in df.columns and len(df) >= 20:
            latest_vol = df["volume"].iloc[-1]
            avg_vol_20 = df["volume"].iloc[-20:].mean()
            if avg_vol_20 > 0 and (latest_vol / avg_vol_20) > VOLUME_ANOMALY_RATIO:
                volume_anomaly = True

        return round(momentum_5d, 2), volume_anomaly

    @staticmethod
    def _calc_conversion_premium(
        cb_price: float, stock_price: float, conversion_price: float
    ) -> float:
        """计算转股溢价率（百分比）。"""
        if conversion_price <= 0 or stock_price <= 0:
            return 999.0
        conversion_value = 100 / conversion_price * stock_price
        if conversion_value <= 0:
            return 999.0
        return (cb_price / conversion_value - 1) * 100

    @staticmethod
    def _classify_status(
        is_triggered: bool,
        trigger_progress: float,
        stock_price_above_130pct: bool,
        trigger_days_count: int,
        redeem_status_cn: str = "",
    ) -> str:
        """
        根据当前状态判断状态标签。

        优先级:
        1. redeem_status_cn 映射（"已公告强赎" → "active_redeeming", "已强赎" → "done"）
        2. 触发天数 >= 阈值 → "triggered"
        3. 价格超阈值且进度 > 0 → "approaching"
        4. 默认 → "watching"

        status_label 含义:
        - watching: 正股低于 130% 转股价，距离触发还远
        - approaching: 正股已满足 130% 条件，正在凑天数（进度 > 0）
        - triggered: 已满足触发条件，但公司尚未公告是否行使赎回权
        - active_redeeming: 公司已公告行使赎回权，转债处于赎回期
        - done: 强赎已完成（已退市或转为股票）
        """
        # 优先检查中文状态映射
        if redeem_status_cn == "已强赎":
            return "done"
        if redeem_status_cn == "已公告强赎":
            return "active_redeeming"

        if is_triggered:
            return "triggered"
        if trigger_progress > 0 and stock_price_above_130pct:
            return "approaching"
        return "watching"

    @staticmethod
    def _empty_result(cb_code: str) -> dict[str, Any]:
        """返回空结果占位。"""
        return {
            "cb_code": cb_code,
            "cb_name": "",
            "is_triggered": False,
            "trigger_progress": 0.0,
            "trigger_days_count": 0,
            "days_remaining": REDEMPTION_THRESHOLD_DAYS,
            "conversion_premium": 999.0,
            "stock_price_above_130pct": False,
            "volume_anomaly": False,
            "status_label": "unknown",
            "cb_price": 0.0,
            "stock_code": "",
            "stock_name": "",
            "stock_price": 0.0,
            "conversion_price": 0.0,
            "trigger_price": 0.0,
            "redemption_price": 0.0,
            "remaining_balance": 0.0,
            "stock_momentum_5d": 0.0,
        }

    @staticmethod
    def _normalize_stock_code(code: str) -> str:
        """
        补全股票代码的交易所前缀。

        akshare 的 stock_zh_a_daily 要求 'sh600036' 或 'sz300969' 格式。
        - 6xxxxx → sh
        - 0xxxxx, 3xxxxx → sz
        - 4xxxxx (三板) → sz
        - 若已有前缀则原样返回。
        """
        code = code.strip()
        if code.startswith(("sh", "sz", "bj")):
            return code
        # 移除可能的点号后缀 (如 "603901.SH")
        if "." in code:
            code = code.split(".")[0]
        if code.startswith("6"):
            return f"sh{code}"
        elif code.startswith(("0", "3", "4")):
            return f"sz{code}"
        else:
            # 未知，保留原样
            return code


# =============================================================================
# LogitScorer —— Logit 回归信号生成器
# =============================================================================


class LogitScorer:
    """
    Logit 回归信号生成器。

    使用 5 个因子 + expert prior 系数，计算强赎概率。

    Logit 模型公式::

        P(redemption) = 1 / (1 + exp(-z))

    其中线性组合::

        z = β0 + β1*x1 + β2*x2 + β3*x3 + β4*x4 + β5*x5

    因子（x1..x5）:
        1. conversion_premium  — 转股溢价率（%），负相关
        2. trigger_progress    — 触发进度 [0, 1]，正相关
        3. stock_momentum_5d   — 正股 5 日动量（%），正相关
        4. log_balance         — 剩余规模的对数 ln(余额)，负相关
        5. is_triggered        — 是否已触发 (0/1)，强正相关
    """

    # Expert prior 系数
    COEFFICIENTS: dict[str, float] = {
        "intercept": -4.0,
        "conversion_premium": -0.08,  # 转股溢价率（越低越可能强赎）
        "trigger_progress": 4.0,  # 触发进度 0-1（越高越可能）
        "stock_momentum_5d": 0.5,  # 正股 5 日动量（上涨的惯性）
        "log_balance": -0.3,  # 剩余规模对数（越小越可能）
        "is_triggered": 3.0,  # 是否已触发（0/1）
    }

    # 信号等级：[(下限, 等级标签), ...]
    # 遍历时按概率从大到小匹配第一个 >= 下限的等级
    SIGNAL_LEVELS: list[tuple[float, str]] = [
        (0.85, "🚨 行动"),  # >= 85%
        (0.60, "🔴 警惕"),  # >= 60%
        (0.30, "🟠 预警"),  # >= 30%
        (0.10, "🟡 关注"),  # >= 10%
        (0.00, "🟢 观望"),  # < 10%
    ]

    def __init__(self, coefficients: dict[str, float] | None = None):
        """
        Parameters
        ----------
        coefficients : dict | None
            可选的系数覆盖。不传则使用 COEFFICIENTS 类变量。
        """
        self.coefficients = dict(self.COEFFICIENTS)
        if coefficients is not None:
            self.coefficients.update(coefficients)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def score_single(self, tracker_output: dict[str, Any]) -> dict[str, Any]:
        """
        对单只转债计算强赎概率。

        输入: RedemptionTracker 的输出 dict

        输出: 在原 dict 基础上增加以下字段:
        {
            **tracker_output,
            "probability": 0.78,         # 强赎概率 [0, 1]
            "signal_level": "🔴 警惕",    # 信号等级
            "signal_score": 78,           # 信号分数 0-100
            "logit_raw": 1.25,            # logit 原始值 z
        }
        """
        # 1. 提取因子值
        x1 = self._safe_float(tracker_output.get("conversion_premium", 0.0))
        x2 = self._safe_float(tracker_output.get("trigger_progress", 0.0))
        x3 = self._safe_float(tracker_output.get("stock_momentum_5d", 0.0))
        x4 = self._calc_log_balance(
            self._safe_float(tracker_output.get("remaining_balance", 0.0))
        )
        x5 = 1.0 if tracker_output.get("is_triggered", False) else 0.0

        # 2. 计算线性组合 z
        coeff = self.coefficients
        z = (
            coeff["intercept"]
            + coeff["conversion_premium"] * x1
            + coeff["trigger_progress"] * x2
            + coeff["stock_momentum_5d"] * x3
            + coeff["log_balance"] * x4
            + coeff["is_triggered"] * x5
        )

        # 3. Sigmoid 变换 → 概率
        probability = self._sigmoid(z)

        # 4. 信号等级 & 分数
        signal_level = self._assign_signal_level(probability)
        signal_score = round(probability * 100, 0)

        # 5. 组装输出
        result = dict(tracker_output)
        result.update(
            {
                "probability": round(probability, 4),
                "signal_level": signal_level,
                "signal_score": int(signal_score),
                "logit_raw": round(z, 4),
            }
        )
        return result

    def batch_score(
        self, tracker_results: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        批量评分。

        Parameters
        ----------
        tracker_results : list[dict]
            RedemptionTracker 批量输出的结果列表。

        Returns
        -------
        list[dict]
            每项已附加 probability / signal_level / signal_score / logit_raw 字段。
        """
        return [self.score_single(item) for item in tracker_results]

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _sigmoid(z: float) -> float:
        """Sigmoid 函数，数值稳定版本。"""
        # 对于极端的正/负值做截断，避免 exp 溢出
        if z > 50:
            return 1.0
        if z < -50:
            return 0.0
        return 1.0 / (1.0 + math.exp(-z))

    @staticmethod
    def _calc_log_balance(balance: float) -> float:
        """
        计算剩余规模的对数 ln(余额)。

        剩余规模为 0 或负数时返回 -10（模拟极小额，使 log_balance 为很大负值 → 提高概率）。
        剩余规模以亿元为单位。
        """
        if balance <= 0:
            return -10.0
        return math.log(balance)

    @staticmethod
    def _safe_float(val: Any) -> float:
        """安全转 float，不可转时返回 0.0。"""
        if val is None:
            return 0.0
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _assign_signal_level(probability: float) -> str:
        """
        根据概率分配信号等级。

        按 SIGANL_LEVELS 从高到低匹配第一个 >= 下限的等级。
        """
        for threshold, level in LogitScorer.SIGNAL_LEVELS:
            if probability >= threshold:
                return level
        return "🟢 观望"


# =============================================================================
# 便捷入口：直接从命令行验证导入
# =============================================================================

if __name__ == "__main__":
    print("✅ strategies.cb_redemption.signals 模块导入成功")
    print(f"   RedemptionTracker: {RedemptionTracker.__doc__.strip().split(chr(10))[0]}")
    print(f"   LogitScorer: {LogitScorer.__doc__.strip().split(chr(10))[0]}")
    print(f"   COEFFICIENTS: {LogitScorer.COEFFICIENTS}")
    print(f"   SIGNAL_LEVELS: {LogitScorer.SIGNAL_LEVELS}")

    # 简单测试
    scorer = LogitScorer()
    # 已触发场景
    result = scorer.score_single({
        "cb_code": "123456",
        "cb_name": "XX转债",
        "is_triggered": True,
        "trigger_progress": 1.0,
        "stock_momentum_5d": 5.0,
        "conversion_premium": 2.5,
        "remaining_balance": 0.5,
    })
    print(f"\n已触发场景: P={result['probability']:.4f}, level={result['signal_level']}, z={result['logit_raw']}")

    # 未触发场景
    result2 = scorer.score_single({
        "cb_code": "789012",
        "cb_name": "YY转债",
        "is_triggered": False,
        "trigger_progress": 0.33,
        "stock_momentum_5d": -1.0,
        "conversion_premium": 45.0,
        "remaining_balance": 5.0,
    })
    print(f"未触发场景: P={result2['probability']:.4f}, level={result2['signal_level']}, z={result2['logit_raw']}")
