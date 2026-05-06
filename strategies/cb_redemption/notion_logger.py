"""
可转债强赎策略 — 优化结果 Notion 入库模块

每次优化迭代后，将结果写入 Notion Raw Inbox，触发编译到 Wiki。

流程：
1. 格式化优化结果（参数、回测指标、对比基线）
2. 创建 Raw Inbox 页面（含 Source URL、Status、Type）
3. 触发 compile-from-raw 编译到 Wiki

用法:
    python -m strategies.cb_redemption.notion_logger --iterations 30 --push
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Notion 客户端 + llmwiki 配置
# ---------------------------------------------------------------------------

NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
LLMWIKI_DIR = os.path.expanduser("~/projects/llmwiki")

# 标准 db id（fallback，优先从 .env 读）
FALLBACK_RAW_DB = "3496e2cd-6e4f-80c6-a8e7-d07863554624"
FALLBACK_WIKI_DB = "3486e2cd-6e4f-81fd-a8e7-d07863554624"


def _load_env():
    """从 llmwiki .env 读取配置。"""
    env_path = os.path.join(LLMWIKI_DIR, ".env")
    config = {}
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                config[k.strip()] = v.strip().strip("'\"").strip("'\"")
    
    return {
        "key": config.get("NOTION_API_KEY", ""),
        "raw_db": config.get("NOTION_RAW_INBOX_DB_ID", FALLBACK_RAW_DB),
        "wiki_db": config.get("NOTION_WIKI_DB_ID", FALLBACK_WIKI_DB),
    }


def notion_request(method: str, path: str, payload: dict | None = None) -> dict:
    """发送 Notion API 请求。"""
    env = _load_env()
    url = f"{NOTION_BASE}/{path}"
    data = json.dumps(payload).encode("utf-8") if payload else None
    
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {env['key']}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        logger.error(f"Notion API error {e.code}: {body}")
        raise


def read_db_schema() -> tuple[dict, str, str, str, str]:
    """读取 Raw 数据库 schema，返回字段映射。"""
    env = _load_env()
    db = notion_request("GET", f"databases/{env['raw_db']}")
    
    props = db.get("properties", {})
    
    title_field = None
    url_field = None
    status_field = None
    select_field = None
    
    for name, prop in props.items():
        t = prop.get("type")
        if t == "title":
            title_field = name
        elif t == "url":
            url_field = name
        elif t == "status":
            status_field = name
        elif t == "select":
            select_field = name
    
    logger.info(f"Schema: title={title_field}, url={url_field}, "
                f"status={status_field}, type={select_field}")
    
    return props, title_field, url_field, status_field, select_field


def get_status_options(props: dict, status_field: str) -> list[str]:
    """获取 Status 字段的可用选项。"""
    status_prop = props.get(status_field, {})
    options = status_prop.get("status", {}).get("options", [])
    return [o["name"] for o in options]


def get_select_options(props: dict, select_field: str) -> list[str]:
    """获取 Type 字段的可用选项。"""
    select_prop = props.get(select_field, {})
    options = select_prop.get("select", {}).get("options", [])
    return [o["name"] for o in options]


def create_raw_entry(
    title: str,
    body_text: str,
    source_url: str = "memo:2026-04-24",
    entry_type: str = "Memo",
    status: str = "Not started",
) -> str:
    """
    创建 Raw Inbox 页面，返回 page_id。
    
    Args:
        title: 页面标题
        body_text: 正文内容（纯文本，自动分 blocks）
        source_url: 来源 URL（无外部来源用 memo:YYYY-MM-DD）
        entry_type: Type select 选项
        status: Status 选项
    
    Returns: 创建的 page_id
    """
    props, title_field, url_field, status_field, select_field = read_db_schema()
    
    if not title_field:
        raise ValueError("未找到 title 类型字段")
    
    # 构建 properties
    properties = {
        title_field: {
            "title": [{"type": "text", "text": {"content": title[:2000]}}]
        },
    }
    
    if url_field:
        properties[url_field] = {"url": source_url}
    if status_field:
        properties[status_field] = {"status": {"name": status}}
    if select_field:
        properties[select_field] = {"select": {"name": entry_type}}
    
    # 构建 children blocks（每块 ~1800 字符）
    children = _text_to_blocks(body_text)
    
    # 分批：首次最多 100 blocks
    env = _load_env()
    first_batch = children[:100]
    remaining = children[100:]
    
    payload = {
        "parent": {"database_id": env["raw_db"]},
        "properties": properties,
        "children": first_batch,
    }
    
    page = notion_request("POST", "pages", payload)
    page_id = page["id"]
    logger.info(f"✅ Raw 页面创建成功: {page_id}")
    
    # 追加剩余 blocks
    batch_num = 1
    while remaining:
        batch = remaining[:100]
        remaining = remaining[100:]
        notion_request(
            "PATCH",
            f"blocks/{page_id}/children",
            {"children": batch},
        )
        batch_num += 1
        logger.info(f"  追加 blocks 批次 {batch_num}")
    
    return page_id


def _text_to_blocks(text: str, max_chars: int = 1800) -> list[dict]:
    """将文本切分为 Notion paragraph blocks。"""
    lines = text.split("\n")
    chunks = []
    current = ""
    
    for line in lines:
        if len(current) + len(line) + 1 > max_chars and current:
            chunks.append(current.strip())
            current = ""
        if current:
            current += "\n"
        current += line
    
    if current.strip():
        chunks.append(current.strip())
    
    blocks = []
    for chunk in chunks:
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": chunk}}]
            },
        })
    
    return blocks


def trigger_compile(page_id: str) -> dict:
    """触发 llmwiki compile-from-raw。"""
    import subprocess
    
    cmd = [
        "python3",
        "scripts/notion_wiki_compiler.py",
        "compile-from-raw",
        page_id,
        "--auto-refine",
    ]
    
    result = subprocess.run(
        cmd,
        cwd=LLMWIKI_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    
    if result.returncode != 0:
        logger.error(f"Compile 失败: {result.stderr[:500]}")
        return {"success": False, "stdout": result.stdout, "stderr": result.stderr}
    
    logger.info(f"✅ Compile 成功: {result.stdout[:300]}")
    return {"success": True, "stdout": result.stdout}


# ---------------------------------------------------------------------------
# 优化结果格式化
# ---------------------------------------------------------------------------

def format_optimization_results(compare: dict) -> str:
    """将优化对比结果格式化为文本。"""
    lines = []
    lines.append(f"# 可转债强赎策略 — 参数优化报告")
    lines.append(f"")
    lines.append(f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"")
    
    baseline = compare.get("baseline", {})
    best = compare.get("best", {})
    improvement = compare.get("improvement", {})
    ranking = compare.get("ranking", [])
    
    # 基线
    lines.append(f"## 基线参数")
    lines.append(f"")
    lines.append(f"- 权重: `{_fmt_weights(baseline.get('weights', []))}`")
    lines.append(f"- 阈值: `{baseline.get('thresholds', {})}`")
    lines.append(f"- 交易数: {baseline.get('total_trades', 0)}")
    lines.append(f"- 胜率: {baseline.get('win_rate', 0)}%")
    lines.append(f"- 平均收益: {baseline.get('avg_return', 0):+.2f}%")
    lines.append(f"- 总盈亏: ¥{baseline.get('total_pnl', 0):+.0f}")
    lines.append(f"")
    
    # 最优
    lines.append(f"## 最优参数")
    lines.append(f"")
    lines.append(f"- 权重: `{_fmt_weights(best.get('weights', []))}`")
    lines.append(f"- 阈值: `{best.get('thresholds', {})}`")
    lines.append(f"- 交易数: {best.get('total_trades', 0)}")
    lines.append(f"- 胜率: {best.get('win_rate', 0)}%")
    lines.append(f"- 平均收益: {best.get('avg_return', 0):+.2f}%")
    lines.append(f"- 总盈亏: ¥{best.get('total_pnl', 0):+.0f}")
    lines.append(f"")
    
    # 改进
    lines.append(f"## 改进幅度")
    lines.append(f"")
    for k, v in improvement.items():
        lines.append(f"- {k}: `{v:+.2f}`")
    lines.append(f"")
    
    # 搜索概况
    lines.append(f"## 搜索概况")
    lines.append(f"")
    lines.append(f"- 迭代次数: {compare.get('n_iterations', 0)}")
    lines.append(f"")
    
    # Top 5
    if ranking:
        lines.append(f"## Top 5 参数组合")
        lines.append(f"")
        lines.append(f"| # | Score | 权重 | 胜率 | 平均收益 | 交易数 |")
        lines.append(f"|---|-------|------|------|---------|--------|")
        for i, r in enumerate(ranking[:5]):
            w = _fmt_weights(r.get("weights", []))
            lines.append(
                f"| {i+1} | {r.get('score', 0):.2f} | `{w}` | "
                f"{r.get('win_rate', 0)}% | {r.get('avg_return', 0):+.2f}% | "
                f"{r.get('total_trades', 0)} |"
            )
    
    lines.append(f"")
    lines.append(f"---")
    lines.append(f"*自动生成 by Hermes Optimization Loop*")
    
    return "\n".join(lines)


def _fmt_weights(w: list) -> str:
    return ", ".join(f"{x:.2f}" for x in w)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_logger(compare: dict, push_to_notion: bool = True, compile: bool = True) -> dict:
    """
    执行优化结果入库流程。
    
    Args:
        compare: optimizer.compare() 的结果 dict
        push_to_notion: 是否写入 Notion
        compile: 是否触发编译
    
    Returns: {"page_id": "...", "compiled": bool}
    """
    text = format_optimization_results(compare)
    title = f"强赎策略优化 | {datetime.now().strftime('%m-%d %H:%M')} | {compare.get('n_iterations', 0)} iter"
    
    if not push_to_notion:
        logger.info("Notion 推送已禁用（--no-push）")
        logger.info(f"标题: {title}")
        logger.info(f"正文:\n{text[:500]}...")
        return {"page_id": None, "compiled": False}
    
    # 创建 Raw Inbox 页面
    page_id = create_raw_entry(
        title=title,
        body_text=text,
        source_url=f"memo:{datetime.now().strftime('%Y-%m-%d')}",
        entry_type="Memo",
        status="Not started",
    )
    
    result = {"page_id": page_id, "compiled": False}
    
    # 触发编译
    if compile:
        compile_result = trigger_compile(page_id)
        result["compiled"] = compile_result["success"]
        result["compile_output"] = compile_result.get("stdout", "")[:500]
    
    logger.info(f"✅ 入库完成: page_id={page_id}, compiled={result['compiled']}")
    return result


# CLI 入口
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    
    # 从 optimizer 生成对比结果并导入
    from strategies.cb_redemption.optimizer import run_optimization
    
    parser = argparse.ArgumentParser(description="优化结果入库 Notion")
    parser.add_argument("--iterations", type=int, default=30, help="优化迭代次数")
    parser.add_argument("--score", choices=["balanced", "aggressive", "conservative"],
                        default="balanced")
    parser.add_argument("--push", action="store_true", help="推送到 Notion")
    parser.add_argument("--no-compile", action="store_true", help="不触发编译")
    args = parser.parse_args()
    
    # 先跑优化
    logger.info("🚀 运行参数优化...")
    compare = run_optimization(iterations=args.iterations, score_mode=args.score, verbose=False)
    
    # 入库
    result = run_logger(
        compare=compare,
        push_to_notion=args.push,
        compile=not args.no_compile,
    )
    
    if result["page_id"]:
        logger.info(f"📄 Notion Page: https://notion.so/{result['page_id'].replace('-', '')}")
