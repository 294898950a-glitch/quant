"""
DeepSeek V3.2 AI 持有有人说分析模块

输入: 一只股票的时间序列 (持有人名称 + 类型 + 比例 + 日期)
输出: 结构化 JSON — 5个字段

用法:
  from scripts.ai_holder_analysis import analyze_stock_holders
  result = analyze_stock_holders(timeseries_for_one_stock)
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("ai_holder")

# 加载 API key (优先 parse.env，其次 .env)
def _load_api_key():
    for path in [
        Path.home() / ".hermes" / ".env",
        Path.home() / "parse.env",
        Path.home() / "projects" / "quant" / ".env",
    ]:
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    return os.environ.get("DEEPSEEK_API_KEY", "")

DEEPSEEK_KEY = _load_api_key()
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

if not DEEPSEEK_KEY:
    log.warning("⚠️  DEEPSEEK_API_KEY 未配置，AI 分析将跳过")


SYSTEM_PROMPT = """你是可转债持有人行为分析师。
输入一只股票多期报告的TOP3持有人的姓名、类型、持股比例。
判断模式并输出JSON，只输出JSON不解释。

JSON字段:
- holder_type: 当前主导持有人的类型 (industrial_capital | mutual_fund | individual | foreign | mixed | unknown)
- stability: 持有人是否稳定 (stable | moderate | volatile | unknown)
- reduction: 减持信号强度 (strong | moderate | none | accumulating | unknown)  
  - strong: 大股东持续大幅减持(>5%绝对值)
  - accumulating: 大股东持续增持
- is_original: 当前大股东是否为原始配售方/产业资本 (true | false | unknown)
- signal: 对强赎的总体判断 (bullish | bearish | neutral | unknown)
  - bullish: 大股东有动力推动强赎(如大量持有且减持中)
  - bearish: 大股东无动力或已清仓
  - neutral: 信息不足或不明确

判断逻辑:
- industrial_capital/原始大股东 减持 → 强赎压力大 → bullish
- mutual_fund 买入/增持 → 纯配置行为 → neutral/bearish
- 自然人游资 → 快进快出 → 弱信号 → neutral"""


def analyze_stock_holders(ts_df) -> dict:
    """对一只股票的持有人时间序列做AI分析。返回5字段dict。"""
    if not DEEPSEEK_KEY or ts_df.empty:
        return {"holder_type": "unknown", "stability": "unknown",
                "reduction": "unknown", "is_original": "unknown", "signal": "unknown"}

    # 构建精简文本：每期报告的 top3 持有人
    lines = []
    for _, row in ts_df.iterrows():
        date = str(row.get("ann_date", "?"))[:10]
        lines.append(f"\n--- {date} ---")
        holders = row.get("holders", [])
        if isinstance(holders, str):
            try:
                holders = json.loads(holders)
            except Exception:
                holders = []
        for i, h in enumerate(holders[:3]):
            name = h.get("name", "?")[:40]
            nature = h.get("nature", "?")
            ratio = h.get("ratio", "?")
            lines.append(f"  #{i+1} {name} | {nature} | {ratio}%")

    text = "\n".join(lines)
    if len(lines) < 3:
        return {"holder_type": "unknown", "stability": "unknown",
                "reduction": "unknown", "is_original": "unknown", "signal": "unknown"}

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"分析这只股票的持有人变化:\n{text}"}
        ],
        "max_tokens": 200,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    import urllib.request
    try:
        req = urllib.request.Request(
            DEEPSEEK_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {DEEPSEEK_KEY}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"]
            # parse JSON from response
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("\n", 1)[0]
            parsed = json.loads(content)
            # ensure all 5 fields
            defaults = {"holder_type": "unknown", "stability": "unknown",
                       "reduction": "unknown", "is_original": "unknown", "signal": "unknown"}
            return {**defaults, **parsed}
    except Exception as e:
        log.warning(f"AI analysis failed: {e}")
        return {"holder_type": "unknown", "stability": "unknown",
                "reduction": "unknown", "is_original": "unknown", "signal": "unknown"}


def analyze_all_stocks(timeseries_df, delay=0.3) -> list[dict]:
    """对所有股票的持有人时间序列做AI分析。返回list of dict。"""
    results = []
    stocks = sorted(timeseries_df["stock_code"].unique())
    log.info(f"AI analysis: {len(stocks)} stocks...")

    for i, code in enumerate(stocks):
        ts = timeseries_df[timeseries_df["stock_code"] == code].sort_values("ann_date")
        r = analyze_stock_holders(ts)
        r["stock_code"] = code
        results.append(r)
        if i % 10 == 0:
            log.info(f"  AI progress: {i+1}/{len(stocks)}")
        time.sleep(delay)

    log.info(f"AI analysis done: {len(results)} stocks")
    return results


if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    import pandas as pd
    df = pd.read_parquet(str(Path.home() / "projects/quant/strategies/cb_redemption/output/holder_records_v2.parquet"))
    meta = pd.read_parquet(str(Path.home() / "projects/quant/strategies/cb_redemption/output/announcement_metadata.parquet"))
    meta["ann_date"] = pd.to_datetime(meta["announcement_time"], unit="ms")
    ts = df.merge(meta[["announcement_id", "ann_date"]], on="announcement_id")
    ts = ts.dropna(subset=["ann_date"]).sort_values(["stock_code", "ann_date"])

    # Test first 3 stocks
    codes = ts["stock_code"].unique()[:3]
    for code in codes:
        sub = ts[ts["stock_code"] == code]
        result = analyze_stock_holders(sub)
        print(f"\n{code}:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
