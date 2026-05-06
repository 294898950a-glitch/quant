#!/usr/bin/env python3
"""
可转债数据仓库构建脚本

从 Tushare Pro（第三方代理 http://tsy.xiaodefa.cn）全量拉取历史数据，
建成本地 Parquet 数据仓库，到期后离线可用。

数据源:
  - cb_basic:   可转债基础信息（静态，一次性）
  - cb_daily:   可转债日线行情（含cb_value, cb_over_rate, remain_size）
  - cb_call:    可转债赎回公告
  - cb_price_chg: 转股价变动历史

存储结构:
  ~/projects/quant/data/cb_warehouse/
    cb_basic.parquet
    cb_daily.parquet          # 全量历史追加
    cb_call.parquet
    cb_price_chg/             # 每只转债一个 parquet
      {ts_code}.parquet

用法:
  python scripts/build_cb_warehouse.py             # 全量拉取
  python scripts/build_cb_warehouse.py --daily     # 只拉cb_daily
  python scripts/build_cb_warehouse.py --update    # 增量更新（仅拉最新交易日）
"""

import argparse
import os
import sys
import time
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# =============================================================================
# 配置
# =============================================================================

TUSHARE_TOKEN = "daae3e3dce4a1af39fbd23d5de5114afce35c9d5c135f711bc9596ee"
TUSHARE_URL = "http://tsy.xiaodefa.cn"

ROOT_DIR = Path(__file__).resolve().parent.parent
WAREHOUSE_DIR = ROOT_DIR / "data" / "cb_warehouse"
WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)

RATE_LIMIT_DELAY = 0.5  # 每次请求间隔秒数（<=120次/分钟即可）
BATCH_SIZE = 2000  # Tushare单次返回上限（实际按天拉取不受此限）

# =============================================================================
# Tushare 客户端
# =============================================================================

class TushareClient:
    def __init__(self, token: str, url: str):
        import tushare as ts
        ts.set_token(token)
        self.pro = ts.pro_api()
        self.pro._DataApi__http_url = url

    def call(self, api_name: str, **kwargs) -> pd.DataFrame:
        """通用API调用，带重试和限速"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                method = getattr(self.pro, api_name)
                df = method(**kwargs)
                time.sleep(RATE_LIMIT_DELAY)
                return df if df is not None else pd.DataFrame()
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = (attempt + 1) * 2
                    print(f"  ⚠️  {api_name} 重试 {attempt+1}/{max_retries}: {e}, 等待{wait}s")
                    time.sleep(wait)
                else:
                    print(f"  ❌ {api_name} 失败: {e}")
                    return pd.DataFrame()


# =============================================================================
# 数据拉取函数
# =============================================================================

def pull_cb_basic(client: TushareClient) -> pd.DataFrame:
    """拉取可转债基础信息（静态表，一次性全量）"""
    print("📦 拉取 cb_basic（可转债基础信息）...")
    fields = (
        "ts_code,bond_short_name,stk_code,stk_short_name,"
        "issue_size,remain_size,conv_price,value_date,maturity_date,list_date"
    )
    df = client.call("cb_basic", fields=fields)
    if not df.empty:
        # 转数值类型
        for col in ["issue_size", "remain_size", "conv_price"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        # 加时间戳
        df["_updated_at"] = datetime.now().isoformat()
        path = WAREHOUSE_DIR / "cb_basic.parquet"
        df.to_parquet(path, index=False)
        print(f"  ✅ {len(df)} 条 -> {path}")
    return df


def pull_cb_daily_range(
    client: TushareClient,
    start_date: str,
    end_date: str,
    existing: set[str] | None = None,
) -> pd.DataFrame:
    """
    拉取 cb_daily 历史行情。

    cb_daily 单次最多2000条（约4-6天），所以按天拉取。

    Args:
        start_date: 开始日期 YYYYMMDD
        end_date: 结束日期 YYYYMMDD
        existing: 已有的 (ts_code, trade_date) 集合，跳过已拉取的数据
    """
    print(f"📦 拉取 cb_daily {start_date} ~ {end_date} ...")

    all_dfs = []
    total_new = 0
    total_skipped = 0

    # 生成交易日列表（逐个交易日拉取，避免2000限制）
    # 先获取交易日历
    try:
        trade_cal = client.call("trade_cal", start_date=start_date, end_date=end_date)
        if trade_cal.empty:
            # 备用方案：遍历所有日期
            print("  ⚠️  交易日历为空，按天轮询")
            trade_dates = []
            d = pd.Timestamp(start_date)
            end = pd.Timestamp(end_date)
            while d <= end:
                if d.weekday() < 5:  # 简单周末过滤
                    trade_dates.append(d.strftime("%Y%m%d"))
                d += timedelta(days=1)
        else:
            trade_dates = sorted(trade_cal[trade_cal["is_open"] == 1]["cal_date"].tolist())
    except Exception:
        print("  ⚠️  交易日历失败，按天轮询")
        trade_dates = []
        d = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        while d <= end:
            if d.weekday() < 5:
                trade_dates.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)

    print(f"  📅 共计 {len(trade_dates)} 个交易日待处理")

    for i, date_str in enumerate(trade_dates):
        # 检查是否已存在
        if existing and date_str in existing:
            total_skipped += 1
            continue

        df = client.call(
            "cb_daily",
            trade_date=date_str,
            fields="ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount,cb_value,cb_over_rate,remain_size",
        )
        if not df.empty:
            all_dfs.append(df)
            total_new += len(df)

        if (i + 1) % 50 == 0:
            print(f"  📊 进度: {i+1}/{len(trade_dates)} 天, 已获 {total_new} 条")

    if not all_dfs:
        print("  ℹ️  无新数据")
        return pd.DataFrame()

    print(f"  ✅ 新增 {total_new} 条 ({len(trade_dates)} 天, {total_new // max(len(trade_dates),1):.0f} 条/天)")
    print(f"  ⏩ 跳过 {total_skipped} 天（已存在）")
    return pd.concat(all_dfs, ignore_index=True)


def pull_cb_call_all(client: TushareClient) -> pd.DataFrame:
    """拉取全部 cb_call 赎回公告（按年分段，覆盖2018-2026）"""
    print("📦 拉取 cb_call（可转债赎回公告）...")
    all_dfs = []
    total = 0
    for year in range(2018, 2027):
        df = client.call(
            "cb_call",
            start_date=f"{year}0101",
            end_date=f"{year}1231",
            fields="ts_code,ann_date,call_date,call_type,is_call,call_price,call_price_tax,call_vol,call_amount,payment_date,call_reg_date",
        )
        if not df.empty:
            all_dfs.append(df)
            total += len(df)
        print(f"  {year}: {len(df)} 条")
    if all_dfs:
        result = pd.concat(all_dfs, ignore_index=True)
        path = WAREHOUSE_DIR / "cb_call.parquet"
        result.to_parquet(path, index=False)
        print(f"  ✅ {total} 条 -> {path}")
    return pd.concat(all_dfs, ignore_index=True) if all_dfs else pd.DataFrame()


def pull_cb_price_chg_all(client: TushareClient) -> pd.DataFrame:
    """
    拉取全部 cb_price_chg 转股价变动。
    每只转债单独拉取，存储为单独文件。
    """
    print("📦 拉取 cb_price_chg（转股价变动历史）...")
    chg_dir = WAREHOUSE_DIR / "cb_price_chg"
    chg_dir.mkdir(parents=True, exist_ok=True)

    # 获取所有转债代码
    df_basic = client.call("cb_basic", fields="ts_code")
    if df_basic.empty:
        print("  ❌ 无法获取转债列表")
        return pd.DataFrame()

    codes = df_basic["ts_code"].tolist()
    print(f"  📋 共计 {len(codes)} 只转债")

    all_records = []
    succeeded = 0
    failed = 0
    skipped = 0

    for i, code in enumerate(codes):
        # 检查是否已拉取
        code_path = chg_dir / f"{code.replace('.', '_')}.parquet"
        if code_path.exists():
            skipped += 1
            if (i + 1) % 200 == 0:
                print(f"  📊 进度: {i+1}/{len(codes)} (成功{succeeded}, 跳过{skipped}, 失败{failed})")
            continue

        try:
            df = client.call(
                "cb_price_chg",
                ts_code=code,
                fields="ts_code,change_date,convert_price_bef,convert_price_aft,change_reason",
            )
            if not df.empty:
                df.to_parquet(code_path, index=False)
                all_records.append(df)
                succeeded += 1
            else:
                # 空数据也存个空文件标记已处理
                pd.DataFrame().to_parquet(code_path)
                skipped += 1
        except Exception as e:
            print(f"  ❌ {code}: {str(e)[:50]}")
            failed += 1

        if (i + 1) % 100 == 0:
            print(f"  📊 进度: {i+1}/{len(codes)} (成功{succeeded}, 跳过{skipped}, 失败{failed})")

    print(f"  ✅ 完成: 成功{succeeded}, 跳过{skipped}, 失败{failed}")
    
    if all_records:
        merged = pd.concat(all_records, ignore_index=True)
        path = WAREHOUSE_DIR / "cb_price_chg.parquet"
        merged.to_parquet(path, index=False)
        print(f"  ✅ 合并文件: {len(merged)} 条 -> {path}")
        return merged
    return pd.DataFrame()


def pull_stock_daily_batch(
    client: TushareClient,
    stock_codes: list[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """
    批量拉取正股日线行情（用于计算正股动量和转股价值）。

    减少调用次数：按正股代码分别拉取。

    Args:
        stock_codes: 正股代码列表
        start_date: 开始日期 YYYYMMDD
        end_date: 结束日期 YYYYMMDD
    """
    print(f"📦 拉取正股日线 {len(stock_codes)} 只, {start_date}~{end_date}...")
    stock_dir = WAREHOUSE_DIR / "stock_daily"
    stock_dir.mkdir(parents=True, exist_ok=True)

    succeeded = 0
    skipped = 0
    for i, code in enumerate(stock_codes):
        code_path = stock_dir / f"{code.replace('.', '_')}.parquet"
        if code_path.exists():
            skipped += 1
            continue
        try:
            df = client.call(
                "daily",
                ts_code=code,
                start_date=start_date,
                end_date=end_date,
                fields="ts_code,trade_date,open,high,low,close,pre_close,vol,amount,pct_chg",
            )
            if not df.empty:
                df.to_parquet(code_path, index=False)
                succeeded += 1
            else:
                pd.DataFrame().to_parquet(code_path)
                skipped += 1
        except Exception as e:
            print(f"  ❌ {code}: {str(e)[:50]}")

        if (i + 1) % 50 == 0:
            print(f"  📊 进度: {i+1}/{len(stock_codes)} (成功{succeeded}, 跳过{skipped})")

    print(f"  ✅ 正股日线完成: 成功{succeeded}, 跳过{skipped}")
    return pd.DataFrame()


# =============================================================================
# 存储和刷新
# =============================================================================

def append_to_warehouse(df: pd.DataFrame, name: str):
    """追加数据到仓库（合并去重）"""
    path = WAREHOUSE_DIR / f"{name}.parquet"
    existing = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    
    if existing.empty:
        combined = df
    else:
        combined = pd.concat([existing, df], ignore_index=True)
        # 按 (ts_code, trade_date) 去重
        if "trade_date" in combined.columns:
            combined = combined.drop_duplicates(subset=["ts_code", "trade_date"], keep="last")
    
    combined = combined.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    combined.to_parquet(path, index=False)
    print(f"  📦 仓库 {name}: {len(df)} 条追加 -> {len(combined)} 条总计")


def get_existing_dates() -> set[str]:
    """获取已拉取的交易日列表"""
    path = WAREHOUSE_DIR / "cb_daily.parquet"
    if not path.exists():
        return set()
    df = pd.read_parquet(path, columns=["trade_date"])
    return set(df["trade_date"].unique())


def summary():
    """打印仓库统计"""
    print("\n" + "=" * 60)
    print("📊 数据仓库状态")
    print("=" * 60)
    
    for name in ["cb_basic", "cb_daily", "cb_call"]:
        path = WAREHOUSE_DIR / f"{name}.parquet"
        if path.exists():
            size_mb = path.stat().st_size / (1024 * 1024)
            df = pd.read_parquet(path)
            print(f"  {name}: {len(df):>8,} 条 ({size_mb:.1f}MB)")
            if "trade_date" in df.columns:
                print(f"         日期: {df['trade_date'].min()} ~ {df['trade_date'].max()}")
            if "ts_code" in df.columns:
                print(f"         转债数: {df['ts_code'].nunique()}")
        else:
            print(f"  {name}: ❌ 未拉取")

    chg_dir = WAREHOUSE_DIR / "cb_price_chg"
    if chg_dir.exists():
        count = len(list(chg_dir.glob("*.parquet")))
        print(f"  cb_price_chg: {count} 只转债已处理")

    stock_dir = WAREHOUSE_DIR / "stock_daily"
    if stock_dir.exists():
        count = len(list(stock_dir.glob("*.parquet")))
        print(f"  stock_daily: {count} 只正股已处理")


# =============================================================================
# 主流程
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="可转债数据仓库构建")
    parser.add_argument("--daily", action="store_true", help="只拉取 cb_daily")
    parser.add_argument("--update", action="store_true", help="增量更新（拉取最新交易日）")
    parser.add_argument("--stock", action="store_true", help="同时拉取正股日线")
    parser.add_argument("--basic", action="store_true", help="只拉取 cb_basic")
    parser.add_argument("--call", action="store_true", help="只拉取 cb_call")
    parser.add_argument("--price-chg", action="store_true", help="只拉取 cb_price_chg")
    parser.add_argument("--summary", action="store_true", help="只查看仓库状态")
    args = parser.parse_args()

    # 初始化客户端
    client = TushareClient(TUSHARE_TOKEN, TUSHARE_URL)

    if args.summary:
        summary()
        return

    # 全量模式
    is_all = not any([args.daily, args.basic, args.call, args.price_chg, args.stock])

    if is_all or args.basic:
        pull_cb_basic(client)

    if is_all or args.call:
        pull_cb_call_all(client)

    if is_all or args.daily or args.update:
        if args.update:
            # 增量：拉取最近5个交易日
            today = datetime.now().strftime("%Y%m%d")
            five_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
            df = pull_cb_daily_range(client, five_days_ago, today)
            if not df.empty:
                append_to_warehouse(df, "cb_daily")
        else:
            # 全量：2018-01-01 到今天
            existing = get_existing_dates() if not is_all else set()
            df = pull_cb_daily_range(client, "20180101", datetime.now().strftime("%Y%m%d"), existing=existing)
            if not df.empty:
                append_to_warehouse(df, "cb_daily")

    if is_all or args.price_chg:
        pull_cb_price_chg_all(client)

    if args.stock:
        # 正股列表
        basic = pd.read_parquet(WAREHOUSE_DIR / "cb_basic.parquet") if (WAREHOUSE_DIR / "cb_basic.parquet").exists() else pull_cb_basic(client)
        stock_codes = basic["stk_code"].dropna().unique().tolist()
        stock_codes = [c for c in stock_codes if c != ""]
        pull_stock_daily_batch(client, stock_codes, "20180101", datetime.now().strftime("%Y%m%d"))

    print("\n" + "=" * 60)
    print("✅ 数据仓库构建完成！")
    print("=" * 60)
    summary()


if __name__ == "__main__":
    main()
