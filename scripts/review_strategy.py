"""可转债强赎策略代码评审脚本
调用 DeepSeek Pro 对整个策略代码库做结构化评审，输出优化方向指导。

用法：
    python scripts/review_strategy.py                  # 评审并打印
    python scripts/review_strategy.py --save            # 保存到本地文件
    python scripts/review_strategy.py --push            # 推送到 Telegram
"""

import json
import os
import sys
import time
from pathlib import Path

# 项目根目录 — 兼容 cron 环境（cron 的 CWD 可能不在项目目录）
_HERMES_ROOT = Path.home() / ".hermes"
_default_root = Path(__file__).resolve().parent.parent

# 如果策略目录不存在，尝试走 cron 常用路径
if (_default_root / "strategies" / "cb_redemption").exists():
    ROOT = _default_root
elif (_default_root.parent / "strategies" / "cb_redemption").exists():
    ROOT = _default_root.parent
else:
    # fallback: explicit project path
    ROOT = Path.home() / "projects" / "quant"

STRATEGY_DIR = ROOT / "strategies" / "cb_redemption"


def collect_code_context() -> str:
    """收集所有代码文件，拼接成结构化上下文。"""
    lines = []

    # 1. 项目结构
    lines.append("# 项目文件结构")
    lines.append("```")
    for f in sorted(STRATEGY_DIR.rglob("*.py")):
        rel = f.relative_to(ROOT)
        loc = len(f.read_text().splitlines())
        lines.append(f"  {rel}  ({loc} lines)")
    lines.append("```\n")

    # 2. 各文件内容
    for f in sorted(STRATEGY_DIR.rglob("*.py")):
        if f.name == "__init__.py":
            continue
        rel = f.relative_to(ROOT)
        content = f.read_text()
        lines.append(f"\n## `{rel}`\n```python\n{content}\n```\n")

    # 3. 数据审计摘要
    lines.append("\n## 数据质量审计摘要\n")
    lines.append("""
| 数据源 | 状态 | 详情 |
|--------|------|------|
| 强赎快照 (bond_cb_redeem_jsl) | ✅ 341只 | 强赎天计数/转股价/正股价 100%非空 |
| 转债日线 (bond_zh_hs_cov_daily) | ✅ 每只200~1200+天 | 覆盖足够回测 |
| 正股日线 (stock_zh_a_daily) | ✅ 近3个月73行 | 够算5日动量 |
| 溢价率 | ⚠️ 自行计算 | API无列，用正股价/转股价算 |
| 强赎状态 | ✅ 4类 | 已公告强赎(10), 公告要强赎(2), 公告不强赎(55), 空白(274) |

**因子使用现状:**
- redeem_progress: ✅ 回测使用，强赎天计数/30
- premium_ratio: ✅ 回测使用，自行计算
- remaining_size: ✅ 回测使用
- stock_momentum: ❌ 回测中权重=0，signals.py有实现但未验证
- market_sentiment: ❌ 两个模块都未实现
""")

    return "\n".join(lines)


def call_deepseek(prompt: str) -> str:
    """通过 Hermes terminal 调用 DeepSeek Pro API。
    使用已配置的环境变量和 curl 直接调用。
    """
    import subprocess

    # 从 .env 读取 DeepSeek API key
    env_path = Path.home() / ".hermes" / ".env"
    api_key = None
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("DEEPSEEK_API_KEY="):
                api_key = line.split("=", 1)[1].strip().strip("\"'")
                break

    if not api_key:
        # 尝试从环境变量
        api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return "❌ 未找到 DEEPSEEK_API_KEY，请在 .hermes/.env 中添加"

    # 构建消息
    system_msg = {
        "role": "system",
        "content": """你是一位量化策略代码评审专家，擅长可转债市场、多因子模型和回测系统设计。

请对以下可转债强赎博弈策略的完整代码库进行结构化评审。

评审重点：
1. **数据质量** — 数据源是否可靠、覆盖是否足够、是否存在未来信息
2. **因子有效性** — 当前3个因子的逻辑是否正确、缺失的2个因子如何补全
3. **回测框架** — 回测设计是否有偏差、是否存在幸存者偏差/前视偏差
4. **优化闭环** — 参数搜索策略是否合理、过拟合风险如何控制
5. **信号生成** — 实时信号与回测是否一致、正股动量如何整合
6. **架构质量** — 代码组织、错误处理、性能瓶颈
7. **可改进方向** — 按 ROI 排序的具体建议

对每个问题给出：
- 严重程度 (Critical / High / Medium / Low)
- 具体问题描述
- 修复建议和预期效果
- 估算工作量 (小时)

最后给出一个按 ROI 排序的优化路线图。"""
    }

    user_msg = {
        "role": "user",
        "content": prompt
    }

    payload = json.dumps({
        "model": "deepseek-chat",
        "messages": [system_msg, user_msg],
        "temperature": 0.3,
        "max_tokens": 8192,
    })

    # 写入临时文件避免 "Argument list too long"
    import tempfile
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    tmp.write(payload)
    tmp.close()

    cmd = [
        "curl", "-s", "-X", "POST",
        "https://api.deepseek.com/chat/completions",
        "-H", f"Authorization: Bearer {api_key}",
        "-H", "Content-Type: application/json",
        "-d", f"@{tmp.name}",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)

    # 清理临时文件
    try:
        os.unlink(tmp.name)
    except Exception:
        pass

    if result.returncode != 0:
        return f"❌ API 调用失败: {result.stderr}"

    try:
        resp = json.loads(result.stdout)
        if "choices" in resp and len(resp["choices"]) > 0:
            return resp["choices"][0]["message"]["content"]
        elif "error" in resp:
            return f"❌ API 错误: {resp['error']['message']}"
        else:
            return f"❌ 未知响应: {json.dumps(resp, indent=2)[:500]}"
    except json.JSONDecodeError as e:
        return f"❌ 响应解析失败: {e}\n{result.stdout[:500]}"


def send_telegram(text: str):
    """通过 Hermes 推送消息到 Telegram。"""
    chat_id = "6403706808"
    token = None
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip("\"'")
                break

    if not token:
        print("⚠️  未找到 TELEGRAM_BOT_TOKEN，跳过推送")
        return

    import urllib.request
    import urllib.parse

    text_preview = text[:4000]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text_preview,
        "parse_mode": "Markdown",
    }).encode()

    try:
        urllib.request.urlopen(url, data=data, timeout=15)
        print("✅ Telegram 推送完成")
    except Exception as e:
        print(f"⚠️  TG 推送失败: {e}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="可转债强赎策略代码评审")
    parser.add_argument("--save", action="store_true", help="保存到本地文件")
    parser.add_argument("--push", action="store_true", help="推送到 Telegram")
    args = parser.parse_args()

    print("📦 收集代码上下文...")
    context = collect_code_context()
    print(f"   共 {len(context)} 字符")

    print("\n🔍 调用 DeepSeek Pro 做代码评审...")
    t0 = time.time()
    review = call_deepseek(context)
    elapsed = time.time() - t0
    print(f"   耗时 {elapsed:.1f}s")
    print(f"   返回 {len(review)} 字符")

    # 输出
    print("\n" + "=" * 60)
    print("📋 代码评审报告")
    print("=" * 60)
    print(review)

    if args.save:
        out_path = ROOT / "reports" / "strategy_review.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(review)
        print(f"\n✅ 已保存到 {out_path}")

    if args.push:
        send_telegram(review)

    return 0


if __name__ == "__main__":
    sys.exit(main())
