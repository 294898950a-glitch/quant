# Quant Wiki — 量化研究知识库

> 基于 **Karpathy llm-wiki** 模式构建的持久化量化知识资产
> LLM 驱动，Notion 托管，持续复利增长

## 🏗️ 架构（三层）

```
┌──────────────────────────────────────────┐
│  Layer 3: SCHEMA.md                      │  ← "宪法"：定义结构/约定/工作流
├──────────────────────────────────────────┤
│  Layer 2: Notion Wiki (LLM 全权维护)      │  ← 持久化知识资产
│  ├─ quant-wiki-index   (总索引)           │
│  ├─ quant-wiki-log     (操作日志)         │
│  ├─ Sources/           (资料摘要页)       │
│  ├─ Entities/          (实体页)           │
│  └─ Syntheses/         (综合分析页)       │
├──────────────────────────────────────────┤
│  Layer 1: data/raw/ (不可变的原始资料)     │  ← 真相来源
│  ├─ joinquant/                       │
│  ├─ papers/                          │
│  ├─ books/                           │
│  └── web-articles/                   │
└──────────────────────────────────────────┘
```

## 📁 项目结构

```
projects/quant/
├── SCHEMA.md                    # ⭐ 知识库"宪法"（必读）
├── README.md                    # 本文件
├── src/
│   ├── notion_push.py           # Notion API 推送引擎 + Block 工厂 + 模板
│   └── ingest.py                # 📥 摄入工具（核心入口）
├── data/
│   ├── raw/                     # 原始资料（不可修改）
│   │   └── joinquant/
│   │       └── 量化入门_均线策略.md
│   └── karpathy-llm-wiki-reference.md  # 参考文档
├── templates/                   # (待建) 页面模板
├── output/                      # 输出
└── logs/                        # 日志
```

## ✅ 已完成

| # | 事项 | 状态 | 说明 |
|---|------|------|------|
| 0 | 项目初始化 + 目录搭建 | ✅ | `src/`, `data/raw/`, `templates/`, `output/`, `logs/` |
| 1 | SCHEMA.md 知识库宪法 | ✅ | 三层架构、操作流程、质量标准 |
| 2 | Notion API 推送引擎 | ✅ | `notion_push.py` — 页面创建/追加/搜索/Block工厂 |
| 3 | Ingest 摄入工具 | ✅ | `ingest.py` — 文本/文件摄入 → Source页面 + Index更新 + Log记录 |
| 4 | Karpathy 参考文档 | ✅ | `data/karpathy-llm-wiki-reference.md` |
| 5 | **端到端测试通过** | ✅ | 真实量化资料已摄入 Notion |

## 🔗 Notion 页面

| 页面 | 类型 | 链接 |
|------|------|------|
| quant-wiki-index | 索引 | [查看](https://www.notion.so/quant-wiki-index-3486e2cd6e4f8117bc14f0f548fe3e10) |
| quant-wiki-log | 日志 | [查看](https://www.notion.so/quant-wiki-log-3486e2cd6e4f811193e6cae9beb567a2) |
| 📄 量化入门：均线策略详解 | Source | [查看](https://www.notion.so/3486e2cd6e4f81b6a2badcdadefac671) |

## 🚀 使用方法

### 摄入一个文件
```bash
cd ~/projects/quant
python3 src/ingest.py \
  --file "data/raw/joinquant/某文章.md" \
  --title "文章标题" \
  --source "来源URL或标识" \
  --tags 标签1 标签2 \
  --entities 实体1 实体2
```

### 摄入一段文本
```bash
python3 src/ingest.py \
  --text "这里是内容..." \
  --title "标题" \
  --source "来源"
```

## 📋 下一步

### Phase 1: 内容体系完善（当前）
- [ ] 定义完整的**分类标签体系**
- [ ] 建立 Entity 页面模板（指标/策略/概念）
- [ ] 接入更多原始资料（聚宽课程、论文等）

### Phase 2: 批量处理
- [ ] 批量摄入脚本（遍历 data/raw/ 目录）
- [ ] 增量更新（跳过已摄入的）
- [ ] 自动提取实体和标签（LLM 分析）

### Phase 3: 自动化
- [ ] Cron 定时同步
- [ ] Telegram 推送通知
- [ ] Lint 健康检查脚本

---

*基于 Karpathy's llm-wiki 模式 | 最后更新: 2026-04-21 by Hermes Agent*
