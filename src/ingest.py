#!/usr/bin/env python3
"""
Quant Wiki — Ingest 摄入工具
将原始资料处理后推送到 Notion Wiki

用法:
  python3 src/ingest.py --file data/raw/joinquant/course_XXX.md
  python3 src/ingest.py --text "这里是原始文本内容" --title "标题" --source "来源"
  python3 src/ingest.py --url "https://example.com/article"
"""

import os
import sys
import json
import argparse
import urllib.request
import urllib.error
from datetime import datetime

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.notion_push import (
    api_request, create_page, append_blocks, search,
    heading2, heading3, paragraph, bulleted_list, numbered_list,
    callout, code_block, divider, table_of_contents, rt,
    PAGES,
)

# === 配置 ===
QUANT_PAGE_ID = PAGES["quant"]  # quant 子页面作为 wiki 入口

# Notion 页面 ID（运行时动态获取或硬编码）
WIKI_INDEX_ID = None   # 索引页 ID
WIKI_LOG_ID = None     # 日志页 ID


def get_or_create_wiki_pages():
    """确保 Index 和 Log 页面存在，返回它们的 ID"""
    global WIKI_INDEX_ID, WIKI_LOG_ID
    
    # 搜索已有的 index 和 log 页面
    result = search("quant-wiki", page_size=10)
    if not result:
        return None, None
    
    for page in result.get("results", []):
        title = "?"
        props = page.get("properties", {})
        for k, v in props.items():
            if v.get("type") == "title":
                t = v.get("title", [])
                if t:
                    title = t[0].get("plain_text", "?")
        
        if "index" in title.lower() and not WIKI_INDEX_ID:
            WIKI_INDEX_ID = page["id"]
        elif "log" in title.lower() and not WIKI_LOG_ID:
            WIKI_LOG_ID = page["id"]
    
    # 如果不存在，创建它们
    if not WIKI_INDEX_ID:
        idx_blocks = [
            heading2("📚 Quant Wiki 知识库索引"),
            paragraph("本页面是量化知识库的总目录。所有摄入的资料和创建的实体都会在此索引。", color="gray"),
            divider(),
            heading3("📄 Sources (资料摘要)"),
            paragraph("_暂无资料_"),
            divider(),
            heading3("🏷️ Entities (实体)"),
            paragraph("_暂无实体_"),
            divider(),
            heading3("🔬 Syntheses (综合分析)"),
            paragraph("_暂无分析_"),
            callout(f"索引创建时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}", "📋", "gray_background"),
        ]
        result = create_page(QUANT_PAGE_ID, "quant-wiki-index", idx_blocks, icon="📚")
        if result:
            WIKI_INDEX_ID = result["id"]
    
    if not WIKI_LOG_ID:
        log_blocks = [
            heading2("📝 Quant Wiki 操作日志"),
            paragraph("所有对知识库的操作都会记录在此页面。", color="gray"),
            divider(),
            callout(f"日志开始: {datetime.now().strftime('%Y-%m-%d %H:%M')}", "🕐", "gray_background"),
        ]
        result = create_page(QUANT_PAGE_ID, "quant-wiki-log", log_blocks, icon="📝")
        if result:
            WIKI_LOG_ID = result["id"]
    
    return WIKI_INDEX_ID, WIKI_LOG_ID


def append_log_entry(operation, description, details=""):
    """追加一条操作日志"""
    _, log_id = get_or_create_wiki_pages()
    if not log_id:
        print("⚠️ 无法获取 Log 页面 ID", file=sys.stderr)
        return
    
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    entry_blocks = [
        heading3(f"[{timestamp}] {operation} | {description}"),
    ]
    if details:
        entry_blocks.append(paragraph(details, color="gray"))
    entry_blocks.append(divider())
    
    append_blocks(log_id, entry_blocks)


def ingest_text(title, content, source="", tags=None, entities=None):
    """
    核心摄入函数：处理一段文本，生成 Source 页面 + Entity 更新
    
    Args:
        title: 资料标题
        content: 原始文本内容
        source: 来源 URL 或标识
        tags: 标签列表
        entities: 提取到的实体列表（可选，不传则由调用方处理）
    
    Returns:
        创建的页面信息
    """
    tags = tags or []
    entities = entities or []
    
    print(f"\n{'='*50}")
    print(f"  📥 Ingest: {title}")
    print(f"  来源: {source or '未知'}")
    print(f"  标签: {', '.join(tags) if tags else '无'}")
    print(f"{'='*50}")
    
    # 1. 确保 index/log 页面存在
    get_or_create_wiki_pages()
    
    # 2. 构建 Source 页面
    meta_callout = f"📅 {datetime.now().strftime('%Y-%m-%d')}  |  "
    meta_callout += f"🏷️ {' '.join(tags)}  |  "
    meta_callout += f"📎 {source or 'local'}"
    
    source_blocks = [
        callout(meta_callout, "📋", "gray_background"),
        divider(),
        heading2("📝 原始摘要"),
        paragraph(content[:2000] if len(content) > 2000 else content),
        divider(),
        heading2("🔑 关键要点"),
    ]
    
    # 关键要点（这里简化处理；实际应由 LLM 先提取）
    # 将内容按段落拆分，取前几个有意义的段落作为要点
    paragraphs = [p.strip() for p in content.split('\n\n') if p.strip() and len(p.strip()) > 20]
    for i, para in enumerate(paragraphs[:7]):
        preview = para[:300] + "..." if len(para) > 300 else para
        source_blocks.append(bulleted_list(preview))
    
    if len(paragraphs) > 7:
        source_blocks.append(paragraph(f"_... 共 {len(paragraphs)} 个段落_", color="gray"))
    
    source_blocks.append(divider())
    
    # 实体链接区域
    if entities:
        source_blocks.append(heading2("🏷️ 相关实体"))
        for entity in entities:
            source_blocks.append(bulleted_list(f"{entity}"))
        source_blocks.append(divider())
    
    # 页脚
    source_blocks.append(
        paragraph(f"— Hermes Auto-ingest | {datetime.now().strftime('%Y-%m-%d %H:%M')} —", color="gray")
    )
    
    # 3. 创建 Source 页面
    result = create_page(
        parent_id=QUANT_PAGE_ID,
        title=f"📄 {title}",
        blocks=source_blocks,
        icon="📥"
    )
    
    if result:
        page_url = result.get('url', '?')
        page_id = result.get('id', '?')
        print(f"\n✅ Source 页面创建成功!")
        print(f"   URL: {page_url}")
        
        # 4. 记录日志
        append_log_entry(
            "ingest",
            title,
            f"页面: {page_url} | 标签: {', '.join(tags) if tags else '无'} | 实体: {len(entities)}"
        )
        
        return {"id": page_id, "url": page_url, "title": title}
    else:
        print("\n❌ 摘要页创建失败")
        return None


def ingest_file(file_path, title=None, **kwargs):
    """从文件摄入"""
    if not os.path.exists(file_path):
        print(f"❌ 文件不存在: {file_path}", file=sys.stderr)
        return None
    
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    
    resolved_title = title or kwargs.pop('title', None) or os.path.basename(file_path)
    return ingest_text(title=resolved_title, content=content, **kwargs)


# CLI 入口
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Quant Wiki Ingest Tool")
    parser.add_argument('--file', help='要摄入的文件路径')
    parser.add_argument('--text', help='直接传入文本内容')
    parser.add_argument('--title', help='资料标题')
    parser.add_argument('--source', help='来源 URL 或标识')
    parser.add_argument('--tags', nargs='*', default=[], help='标签')
    parser.add_argument('--entities', nargs='*', default=[], help='提取的实体')
    
    args = parser.parse_args()
    
    if args.file:
        result = ingest_file(
            args.file,
            title=args.title,
            source=args.source,
            tags=args.tags,
            entities=args.entities,
        )
    elif args.text:
        result = ingest_text(
            title=args.title or "未命名资料",
            content=args.text,
            source=args.source,
            tags=args.tags,
            entities=args.entities,
        )
    else:
        parser.print_help()
        sys.exit(1)
    
    if result:
        print(f"\n🎉 摄入完成!")
    else:
        print(f"\n❌ 摄入失败")
        sys.exit(1)
