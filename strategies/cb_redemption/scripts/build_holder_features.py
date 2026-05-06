#!/usr/bin/env python3
"""
可转债持有人特征构建与因果对齐流水线
=============================================
三步流程:
  1. 解析PDF提取持有人数据 → holder_records.parquet
  2. 与cb_call事件因果对齐（merge_asof, 避免前视偏差）
  3. 计算持有人特征 → cb_holder_features.parquet

关键新特征:
  - top1_ratio, top3_ratio, top5_ratio: 集中度
  - major_shareholder_present: 大股东在持有人名单中
  - top1_ratio_change: 与上一期报告的变动（减持检测）
  - num_holders: top10中的持有人数量
"""

import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("build_holder_features")

# ── 路径配置 ────────────────────────────────────────────────────────
WAREHOUSE_DIR = Path.home() / "projects" / "quant" / "data" / "cb_warehouse"
STRATEGY_DIR = Path.home() / "projects" / "quant" / "strategies" / "cb_redemption"
OUTPUT_DIR = STRATEGY_DIR / "output"
PDF_DIR = STRATEGY_DIR / "pdfs"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════
# Step 1: PDF解析 → holder_records.parquet
# ═══════════════════════════════════════════════════════════════════════

def _parse_holder_table_from_pdf(pdf_path: Path) -> Optional[list[dict]]:
    """
    从PDF中提取"前十名可转债持有人"表格。
    支持多种标题变体。
    返回 [{name, nature, quantity, amount, ratio}] 或 None。
    """
    try:
        import fitz
    except ImportError:
        log.warning("PyMuPDF not installed, skipping PDF parsing")
        return None

    if not pdf_path.exists() or pdf_path.stat().st_size < 100:
        return None

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        log.warning(f"  Cannot open PDF {pdf_path.name}: {e}")
        return None

    table_keywords = [
        "前十名可转换公司债券持有人",
        "前十名可转换债券持有人",
        "前十名可转债持有人",
        "前十名转债持有人",
        "前十名债券持有人",
    ]

    # 找到包含表格的第一页
    start_page = None
    page_texts = []
    for page_idx in range(len(doc)):
        text = doc[page_idx].get_text("text")
        page_texts.append(text)
        for kw in table_keywords:
            if kw in text:
                start_page = page_idx
                break
        if start_page is not None:
            break

    if start_page is None:
        doc.close()
        return None

    # 合并从start_page开始连续几页的文本（最多3页）
    all_lines = []
    for pg in range(start_page, min(start_page + 3, len(doc))):
        text = page_texts[pg] if pg == start_page else doc[pg].get_text("text")
        lines = text.split("\n")
        all_lines.extend(lines)

    doc.close()

    # 找标题行
    header_idx = -1
    for i, line in enumerate(all_lines):
        for kw in table_keywords:
            if kw in line:
                header_idx = i
                break
        if header_idx >= 0:
            break

    if header_idx < 0:
        return None

    # 逐行解析
    data_rows = []
    current_name_parts = []
    current_row = None

    # 表头行跳过关键词
    skip_patterns = re.compile(
        r"^(序号|可转债持有人名称|可转债持有人性|质|"
        r"报告期末持有可|转债数量|转债金额|转债占比|"
        r"可转换公司债券持有人名称)$"
    )

    for line in all_lines[header_idx + 1:]:
        line = line.strip()
        if not line:
            continue

        if skip_patterns.match(line):
            continue

        # 终止符：后续内容不是表格
        if re.search(r"(报告期.*变动|转股.*情况|累计转股|本次变动|赎回|回售)", line):
            if current_row is None or "ratio" not in current_row:
                break
            # 如果当前行已完整，再break

        # 数字序号行 (1-10)
        num_match = re.match(r"^(\d{1,2})\s*$", line)
        if num_match and 1 <= int(num_match.group(1)) <= 10:
            if current_row:
                if current_name_parts:
                    current_row["name"] = "".join(current_name_parts)
                data_rows.append(current_row)
            current_name_parts = []
            current_row = {"seq": int(num_match.group(1))}
            continue

        if current_row is None:
            continue

        # 占比行 "6.96%"
        ratio_match = re.match(r"^([\d.]+)%$", line)
        if ratio_match:
            current_row["ratio"] = float(ratio_match.group(1))
            continue

        # 金额行 "391,797,300.00"
        amount_match = re.match(r"^([\d,]+)\.(\d{2})$", line)
        if amount_match and "amount" not in current_row and "quantity" in current_row:
            current_row["amount"] = float(
                f"{amount_match.group(1).replace(',', '')}.{amount_match.group(2)}"
            )
            continue

        # 数量行（纯数字+逗号）
        qty_match = re.match(r"^([\d,]+)$", line)
        if qty_match:
            val_str = qty_match.group(1).replace(",", "")
            if "quantity" not in current_row:
                current_row["quantity"] = float(val_str)
            elif "amount" not in current_row:
                current_row["amount"] = float(val_str)
            continue

        # 性质行
        nature_keywords = [
            "国有法人", "境内非国有法人", "其他", "境内自然人",
            "境外法人", "境外自然人", "理财产品", "券商",
        ]
        found_nature = False
        for kw in nature_keywords:
            if line == kw or line.startswith(kw):
                current_row["nature"] = kw
                found_nature = True
                break

        if found_nature:
            continue

        # 名称行（多行名称拼接）
        if "quantity" not in current_row:
            if line and not re.match(r"^[\d,.]+$", line):
                current_name_parts.append(line)

    # 保存最后一条
    if current_row:
        if current_name_parts:
            current_row["name"] = "".join(current_name_parts)
        data_rows.append(current_row)

    # 构建结果
    result = []
    for row in data_rows:
        seq = row.get("seq", 0)
        if seq < 1 or seq > 10:
            continue
        name = row.get("name", "")
        name = re.sub(r"\d+$", "", name).strip()
        result.append({
            "name": name,
            "nature": row.get("nature", ""),
            "quantity": float(row.get("quantity", 0)),
            "amount": float(row.get("amount", 0)) if row.get("amount") else 0.0,
            "ratio": float(row.get("ratio", 0)),
        })

    if len(result) >= 3:
        return result
    return None


def parse_all_pdfs(metadata: pd.DataFrame) -> pd.DataFrame:
    """解析所有已下载PDF的持有人表格"""
    holders_list = []
    total = len(metadata)
    for i, (_, row) in enumerate(metadata.iterrows()):
        pdf_path = Path(row["pdf_path"])
        if not pdf_path.exists():
            continue

        table = _parse_holder_table_from_pdf(pdf_path)
        if table is not None:
            holders_list.append({
                "announcement_id": row["announcement_id"],
                "stock_code": row["stock_code"],
                "title": row["title"],
                "category": row["category"],
                "pdf_path": str(pdf_path),
                "num_holders": len(table),
                "holders": json.dumps(table, ensure_ascii=False),
                "top1_ratio": table[0]["ratio"] if len(table) > 0 else 0,
                "top3_ratio": sum(h["ratio"] for h in table[:3]) if len(table) >= 3 else 0,
                "top5_ratio": sum(h["ratio"] for h in table[:5]) if len(table) >= 5 else 0,
                "top10_ratio": sum(h["ratio"] for h in table),
            })

        if (i + 1) % 100 == 0:
            log.info(f"  Parse progress: {i+1}/{total}, found={len(holders_list)}")

    df = pd.DataFrame(holders_list)
    log.info(f"PDF parsing done: {len(df)}/{total} contain holder tables")
    return df


# ═══════════════════════════════════════════════════════════════════════
# Step 2: 提取报告日期 & 映射ts_code
# ═══════════════════════════════════════════════════════════════════════

def extract_report_date(title: str) -> Optional[str]:
    """
    从标题中提取报告日期。
    "2024年年度报告" → 2024-12-31
    "2025年半年度报告" → 2025-06-30
    "2024年年度报告全文" → 2024-12-31
    """
    m = re.search(r"(20\d{2})年(?:年度|半年度)", title)
    if not m:
        return None
    year = int(m.group(1))
    if "半年度" in title:
        return f"{year}-06-30"
    else:
        return f"{year}-12-31"


def build_stock_to_ts_map(cb_basic: pd.DataFrame) -> dict:
    """构建 6-digit stock_code → ts_code 映射"""
    stock_map = {}
    for _, r in cb_basic.iterrows():
        stk = r.get("stk_code", "")
        if pd.isna(stk):
            continue
        num = re.search(r"(\d{6})", str(stk))
        if num:
            # 一个stock可能有多个CB ts_code，取第一个
            key = num.group(1)
            if key not in stock_map:
                stock_map[key] = r["ts_code"]
    return stock_map


# ═══════════════════════════════════════════════════════════════════════
# Step 3: 因果对齐 — merge_asof 避免前视偏差
# ═══════════════════════════════════════════════════════════════════════

def compute_features_and_align(
    holder_records: pd.DataFrame,
    cb_call: pd.DataFrame,
    cb_basic: pd.DataFrame,
) -> pd.DataFrame:
    """
    对每个cb_call事件，找到ann_date之前最新的holder record，
    计算holder特征。严格因果对齐（无前视偏差）。

    返回: 每个cb_call事件 + holder特征的DataFrame
    """
    if holder_records.empty:
        log.warning("No holder records available")
        return pd.DataFrame()

    # 1. 提取report_date
    holder_records = holder_records.copy()
    holder_records["report_date"] = holder_records["title"].apply(extract_report_date)
    holder_records = holder_records.dropna(subset=["report_date"])
    holder_records["report_date"] = pd.to_datetime(holder_records["report_date"])

    log.info(f"Holder records with valid report_date: {len(holder_records)}")

    # 2. 映射ts_code
    stock_map = build_stock_to_ts_map(cb_basic)
    holder_records["stk_num"] = holder_records["stock_code"].astype(str).str[:6]
    holder_records["ts_code"] = holder_records["stk_num"].map(stock_map)
    holder_records = holder_records.dropna(subset=["ts_code"])
    log.info(f"Holder records after ts_code mapping: {len(holder_records)}")

    # 3. 按stock_code+report_date排序
    holder_records = holder_records.sort_values(["ts_code", "report_date"])

    # 4. 计算 major_shareholder_present 特征
    major_keywords = ["集团", "控股", "总公司"]
    fund_keywords = ["基金", "证券", "保险", "养老金", "信托", "资产管理", "理财产品"]

    def check_major_shareholder(holders_json: str) -> bool:
        """检查持有人中是否有大股东（非金融机构）"""
        try:
            holders = json.loads(holders_json)
        except (json.JSONDecodeError, TypeError):
            return False
        for h in holders:
            name = h.get("name", "")
            nature = h.get("nature", "")
            # 排除金融机构
            if any(kw in name for kw in fund_keywords):
                continue
            if nature in ("其他", "境外法人", "境外自然人", ""):
                # 对于"其他"性质，看名称是否含大股东关键词
                if any(kw in name for kw in major_keywords):
                    return True
            if nature in ("国有法人", "境内非国有法人"):
                # 法人都可能是大股东
                if any(kw in name for kw in major_keywords):
                    return True
                # 境内非国有法人也可能是原始股东
                if nature == "境内非国有法人":
                    return True
        return False

    holder_records["major_shareholder_present"] = holder_records["holders"].apply(
        check_major_shareholder
    )

    # 5. 计算 top1_ratio_change (与上一期对比)
    holder_records["top1_ratio_prev"] = holder_records.groupby("ts_code")["top1_ratio"].shift(1)
    holder_records["top1_ratio_change"] = (
        holder_records["top1_ratio"] - holder_records["top1_ratio_prev"]
    )
    # 标记是否减持（top1下降>0.5个百分点）
    holder_records["top1_reducing"] = holder_records["top1_ratio_change"] < -0.5

    # 6. 准备call事件
    events = cb_call.copy()
    events["ann_date_dt"] = pd.to_datetime(events["ann_date"], format="%Y%m%d")
    events = events.sort_values("ann_date_dt")

    # 7. merge_asof: 对每个call事件取ann_date之前最新的holder record
    holders_for_merge = holder_records[[
        "ts_code", "report_date", "num_holders", "holders",
        "top1_ratio", "top3_ratio", "top5_ratio", "top10_ratio",
        "major_shareholder_present", "top1_ratio_change", "top1_reducing",
        "stock_code",
    ]].sort_values("report_date")

    merged = pd.merge_asof(
        events,
        holders_for_merge,
        left_on="ann_date_dt",
        right_on="report_date",
        by="ts_code",
        direction="backward",
        tolerance=pd.Timedelta(days=365 * 3),  # 往前看最多3年
    )

    matched = merged["top1_ratio"].notna().sum()
    log.info(f"Merged: {matched}/{len(merged)} call events have holder data "
             f"({matched/len(merged)*100:.1f}%)")

    return merged


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="持有人特征构建与因果对齐")
    parser.add_argument("--parse-only", action="store_true", help="仅解析PDF")
    parser.add_argument("--output", default=str(WAREHOUSE_DIR / "cb_holder_features.parquet"),
                        help="输出路径")
    args = parser.parse_args()

    # ── 加载数据 ──
    cb_basic = pd.read_parquet(WAREHOUSE_DIR / "cb_basic.parquet")
    cb_call = pd.read_parquet(WAREHOUSE_DIR / "cb_call.parquet")
    log.info(f"Loaded: cb_basic={len(cb_basic)}, cb_call={len(cb_call)}")

    # ── Step 1: 解析PDF → holder_records.parquet ──
    meta_path = OUTPUT_DIR / "announcement_metadata.parquet"
    holder_path = OUTPUT_DIR / "holder_records.parquet"

    if meta_path.exists():
        meta_df = pd.read_parquet(meta_path)
        log.info(f"Loaded {len(meta_df)} announcements from metadata")

        # 也扫描PDF目录中未被metadata收录的PDF
        extra_pdfs = []
        pdf_files = list(PDF_DIR.glob("*/*.pdf"))
        known_pdfs = set()
        if "pdf_path" in meta_df.columns:
            known_pdfs = set(meta_df["pdf_path"].tolist())
        for pdf_file in pdf_files:
            if str(pdf_file) not in known_pdfs:
                # 尝试从目录名推断stock_code
                stock_code = pdf_file.parent.name
                # 从文件名推断信息
                title = pdf_file.stem
                # 简单推断类别
                category = "annual"  # 默认
                extra_pdfs.append({
                    "announcement_id": pdf_file.stem,
                    "stock_code": stock_code,
                    "title": title,
                    "category": category,
                    "pdf_path": str(pdf_file),
                })
        if extra_pdfs:
            extra_df = pd.DataFrame(extra_pdfs)
            log.info(f"Found {len(extra_pdfs)} extra PDFs not in metadata")
            meta_df = pd.concat([meta_df, extra_df], ignore_index=True)

        results = parse_all_pdfs(meta_df)
        if len(results) > 0:
            results.to_parquet(holder_path, index=False)
            log.info(f"Holder records saved: {holder_path} ({len(results)} rows, "
                     f"{results['stock_code'].nunique()} stocks)")
            if "top1_ratio" in results.columns:
                log.info(f"  top1_ratio mean={results['top1_ratio'].mean():.2f}%, "
                         f"top5_ratio mean={results['top5_ratio'].mean():.2f}%")
        else:
            log.warning("No holder tables found in any PDF!")
            # Create empty parquet with correct schema
            results = pd.DataFrame(columns=[
                "announcement_id", "stock_code", "title", "category",
                "pdf_path", "num_holders", "holders",
                "top1_ratio", "top3_ratio", "top5_ratio", "top10_ratio",
            ])
            results.to_parquet(holder_path, index=False)
    else:
        log.warning(f"No metadata at {meta_path}, checking PDF dir directly...")
        # 直接从PDF目录构建
        pdf_files = list(PDF_DIR.glob("*/*.pdf"))
        if pdf_files:
            records = []
            for pdf_file in pdf_files:
                stock_code = pdf_file.parent.name
                records.append({
                    "announcement_id": pdf_file.stem,
                    "stock_code": stock_code,
                    "title": pdf_file.stem,
                    "category": "annual",
                    "pdf_path": str(pdf_file),
                })
            meta_df = pd.DataFrame(records)
            results = parse_all_pdfs(meta_df)
            if len(results) > 0:
                results.to_parquet(holder_path, index=False)
                log.info(f"Holder records saved: {holder_path} ({len(results)} rows)")
            else:
                log.warning("No holder tables found!")
                results = pd.DataFrame()
        else:
            log.warning("No PDFs found at all")
            results = pd.DataFrame()

    if args.parse_only:
        return

    # ── Step 2 & 3: 加载holder_records（刚生成的），计算特征并对齐 ──
    if holder_path.exists():
        holder_records = pd.read_parquet(holder_path)
        log.info(f"Loaded {len(holder_records)} holder records for feature computation")
    else:
        log.error(f"holder_records.parquet not found at {holder_path}")
        sys.exit(1)

    if holder_records.empty:
        log.warning("Holder records empty, cannot compute features")
        return

    # 计算特征并对齐
    merged = compute_features_and_align(holder_records, cb_call, cb_basic)

    if merged.empty:
        log.warning("No events matched with holder data")
        return

    # 保存
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(out_path, index=False)
    log.info(f"Saved cb_holder_features to {out_path} ({len(merged)} rows)")

    # ── 统计摘要 ──
    print("\n" + "=" * 70)
    print("持有人特征汇总统计")
    print("=" * 70)

    n_events = len(merged)
    n_matched = merged["top1_ratio"].notna().sum()
    print(f"  CB call事件总数: {n_events}")
    print(f"  有holder数据的: {n_matched} ({n_matched/n_events*100:.1f}%)")

    if n_matched > 0:
        matched = merged.dropna(subset=["top1_ratio"])
        print(f"\n  特征分布:")
        for col, label in [
            ("top1_ratio", "Top1持有人占比(%)"),
            ("top3_ratio", "Top3持有人占比(%)"),
            ("top5_ratio", "Top5持有人占比(%)"),
            ("top10_ratio", "Top10持有人占比(%)"),
            ("num_holders", "持有人数量"),
        ]:
            if col in matched.columns:
                vals = matched[col].dropna()
                if len(vals) > 0:
                    print(f"    {label}: mean={vals.mean():.2f}, "
                          f"median={vals.median():.2f}, "
                          f"min={vals.min():.2f}, max={vals.max():.2f}")

        if "major_shareholder_present" in matched.columns:
            mp = matched["major_shareholder_present"].sum()
            print(f"\n  大股东在场: {mp}/{n_matched} ({mp/n_matched*100:.1f}%)")

        if "top1_ratio_change" in matched.columns:
            changes = matched["top1_ratio_change"].dropna()
            if len(changes) > 0:
                print(f"  Top1比率变动: mean={changes.mean():.2f}pp, "
                      f"min={changes.min():.2f}pp, max={changes.max():.2f}pp")
                reducing = (changes < -0.5).sum()
                print(f"  减持信号(top1降>0.5pp): {reducing}/{len(changes)} "
                      f"({reducing/len(changes)*100:.1f}%)")

    # ── 大股东样本 ──
    if "major_shareholder_present" in merged.columns:
        mp_cases = merged[merged["major_shareholder_present"] == True]
        if len(mp_cases) > 0:
            print(f"\n{'='*70}")
            print(f"大股东在场样本 ({len(mp_cases)} cases)")
            print(f"{'='*70}")
            for i, (_, row) in enumerate(mp_cases.head(10).iterrows()):
                try:
                    holders = json.loads(row["holders"])
                except:
                    holders = []
                holder_names = [h.get("name", "?") for h in holders[:3]]
                print(f"  {i+1}. ts_code={row['ts_code']}, "
                      f"ann_date={row['ann_date']}, "
                      f"top1={row['top1_ratio']:.1f}%, "
                      f"holders: {', '.join(holder_names)}")

    print(f"\n输出文件: {out_path}")
    print("Done!")


if __name__ == "__main__":
    main()
