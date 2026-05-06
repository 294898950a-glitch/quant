#!/usr/bin/env python3
"""
Quant Research → Notion 推送工具
支持: 创建页面、追加内容、数据库操作
"""

import os
import json
import sys
import urllib.request
import urllib.error
from datetime import datetime

# === 配置 ===
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_VERSION = "2022-06-28"
BASE_URL = "https://api.notion.com/v1"

# 已知页面 ID
PAGES = {
    "investment": "62f13862-3392-4098-94d3-c351ae5232f9",  # 投资思考
    "quant": "3476e2cd-6e4f-80ed-907a-e9931fbc0908",      # quant
}


def _headers():
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def api_request(method, endpoint, data=None):
    """通用 Notion API 请求"""
    url = f"{BASE_URL}{endpoint}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=_headers(), method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"❌ API Error {e.code}: {error_body[:300]}", file=sys.stderr)
        return None


def create_page(parent_id, title, blocks, parent_type="page_id", icon="📊"):
    """在指定父页面下创建新页面"""
    data = {
        "parent": {parent_type: parent_id},
        "icon": {"type": "emoji", "emoji": icon},
        "properties": {
            "title": {
                "title": [{"text": {"content": title}}]
            }
        },
        "children": blocks,
    }
    result = api_request("POST", "/pages", data)
    if result:
        print(f"✅ 页面创建成功: {title}")
        print(f"   URL: {result.get('url', '?')}")
        return result
    return None


def append_blocks(page_id, blocks):
    """向已有页面追加内容块"""
    data = {"children": blocks}
    result = api_request("PATCH", f"/blocks/{page_id}/children", data)
    if result:
        print(f"✅ 追加 {len(blocks)} 个块到 {page_id[:8]}...")
    return result


def search(query="", page_size=10):
    """搜索 Notion 内容"""
    data = {"query": query, "page_size": page_size}
    return api_request("POST", "/search", data)


# === Block 工厂函数 ===

def heading2(text):
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [rt(text)]}}

def heading3(text):
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [rt(text)]}}

def paragraph(text, color=None):
    ann = {"color": color} if color else {}
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [rt(text, annotations=ann)]}}

def bulleted_list(text):
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [rt(text)]}}

def numbered_list(text):
    return {"object": "block", "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": [rt(text)]}}

def divider():
    return {"object": "block", "type": "divider", "divider": {}}

def code_block(code, lang="python"):
    return {"object": "block", "type": "code",
            "code": {"rich_text": [rt(code)], "language": lang}}

def callout(text, emoji="💡", color="default"):
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": [rt(text)],
                       "icon": {"type": "emoji", "emoji": emoji},
                       "color": color}}

def table_of_contents():
    return {"object": "block", "type": "table_of_contents",
            "table_of_contents": {"color": "default"}}

def rt(text, annotations=None):
    """创建 rich text 对象"""
    ann = annotations or {}
    return {"type": "text", "text": {"content": text}, "annotations": ann}


# === 预定义模板 ===

def quant_research_template(title, meta, sections):
    """
    量化研究笔记模板
    
    meta: dict {author, date, tags, status}
    sections: list of {heading, content (list of blocks)}
    """
    blocks = []
    
    # 元信息 callout
    meta_text = f"📅 {meta.get('date', datetime.now().strftime('%Y-%m-%d'))}  |  "
    meta_text += f"🏷️ {' '.join(meta.get('tags', []))}  |  "
    meta_text += f"📊 {meta.get('status', 'draft')}"
    blocks.append(callout(meta_text, "📋", "gray_background"))
    blocks.append(divider())
    
    # 目录
    blocks.append(heading2("目录"))
    blocks.append(table_of_contents())
    blocks.append(divider())
    
    # 各章节
    for section in sections:
        blocks.append(heading2(section["heading"]))
        for block in section["content"]:
            blocks.append(block)
        blocks.append(divider())
    
    # 页脚
    blocks.append(paragraph(
        f"— 由 Hermes Agent 自动生成 | {datetime.now().strftime('%Y-%m-%d %H:%M')} —",
        color="gray"
    ))
    
    return blocks


if __name__ == "__main__":
    # 测试模式: 创建一个示例研究页面
    print("=" * 50)
    print("  Quant → Notion 推送测试")
    print("=" * 50)
    
    test_blocks = quant_research_template(
        title="[TEST] 量化策略框架验证",
        meta={
            "date": "2026-04-21",
            "tags": ["测试", "框架验证", "自动化"],
            "status": "draft"
        },
        sections=[
            {
                "heading": "🎯 研究目标",
                "content": [
                    paragraph("验证 Notion API 推送流程的完整性和可靠性。"),
                    bulleted_list("确认 API 认证与权限"),
                    bulleted_list("测试 Block 渲染效果"),
                    bulleted_list("验证模板系统的灵活性"),
                ]
            },
            {
                "heading": "📐 方法论",
                "content": [
                    paragraph("采用 MVP 方式：先跑通最小可行路径，再逐步扩展功能。"),
                    callout("核心原则：轻量、可复用、易维护", "⚡", "blue_background"),
                    heading3("技术栈"),
                    code_block("Notion API v2022-06-28\nPython 3.12 + urllib\nJSON Block 结构", lang="plain text"),
                ]
            },
            {
                "heading": "📊 测试结果",
                "content": [
                    bulleted_list("✅ API 连接认证正常"),
                    bulleted_list("✅ 页面创建成功"),
                    bulleted_list("✅ 多种 Block 类型渲染正确"),
                    bulleted_list("✅ 模板系统工作正常"),
                    callout("所有测试通过！可以开始正式使用。", "🎉", "green_background"),
                ]
            },
            {
                "heading": "🔄 后续计划",
                "content": [
                    numbered_list("接入量化策略研究资料"),
                    numbered_list("建立分类标签体系"),
                    numbered_list("实现批量推送脚本"),
                    numbered_list("添加定时同步任务"),
                ]
            }
        ]
    )
    
    result = create_page(
        parent_id=PAGES["quant"],
        title="[TEST] 量化策略框架验证",
        blocks=test_blocks,
        icon="🧪"
    )
    
    if result:
        print("\n🎉 测试完成！请检查 Notion 页面。")
    else:
        print("\n❌ 测试失败，请检查日志。")
        sys.exit(1)
