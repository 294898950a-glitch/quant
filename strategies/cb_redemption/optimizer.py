"""
可转债强赎博弈策略 — 参数优化器（闭环版，CMA-ES 引擎）

核心特性：
1. 从持久化文件读取当前最优基线（而非硬编码）
2. 用 CMA-ES（协方差自适应进化策略）进行参数搜索
3. 找到更优参数后自动更新 config.py + 持久化文件
4. 优化结果推送到 Telegram

Usage:
    python -m strategies.cb_redemption.optimizer --generations 5 --push
    python -m strategies.cb_redemption.optimizer --generations 10 --score conservative --json
"""

from __future__ import annotations

import argparse
import json
import pickle
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Any

import numpy as np

from strategies.cb_redemption import config
from strategies.cb_redemption.backtest import (
    DEFAULT_WEIGHTS,
    DEFAULT_THRESHOLDS,
    BacktestEngine,
    calc_performance,
)

logger = logging.getLogger(__name__)

# =============================================================================
# 参数搜索空间
# =============================================================================

WEIGHT_RANGES = [
    (0.5, 5.0),     # w0: redeem_progress
    (-5.0, -0.5),   # w1: premium_ratio
    (-4.0, -0.5),   # w2: remaining_size
    (-1.0, 2.0),    # w3: stock_momentum
    (-1.0, 2.0),    # w4: market_sentiment
]

# CMA-ES 参数（5 维）
# popsize = 4 + floor(3 * ln(n)) = 4 + 3*ln(5) ≈ 8.8 → 10
CMA_POPSIZE = 10


# =============================================================================
# 数据模型
# =============================================================================

@dataclass
class OptimizerResult:
    """单次优化迭代结果"""
    weights: list[float]
    thresholds: dict[str, float]
    total_trades: int
    win_rate: float
    avg_return: float
    total_pnl: float
    max_return: float
    min_return: float
    score: float
    elapsed: float
    timestamp: str = ""


# =============================================================================
# 基线持久化
# =============================================================================

def load_baseline() -> tuple[list[float], dict[str, float] | None]:
    """从持久化文件读取当前最优基线。

    Returns:
        (weights, thresholds)
        thresholds=None 表示使用默认阈值
    """
    path = config.OPTIMIZER_BASELINE_FILE
    n_expected = len(WEIGHT_RANGES)

    def _pad_weights(w: list[float]) -> list[float]:
        """补齐缺失权重（当新增因子时自动填 0.1）。"""
        if len(w) < n_expected:
            missing = n_expected - len(w)
            logger.info(f"📌 权重 {len(w)}→{n_expected} 维，新增 {missing} 个初始化为 0.1")
            return list(w) + [0.1] * missing
        return list(w)

    if not os.path.exists(path):
        logger.info("无持久化基线文件，使用 config.py 初始值")
        return _pad_weights(list(config.LOGIT_WEIGHTS)), dict(config.DEFAULT_THRESHOLDS_CONFIG)

    try:
        with open(path) as f:
            data = json.load(f)
        w = data.get("weights", config.LOGIT_WEIGHTS)
        t = data.get("thresholds", config.DEFAULT_THRESHOLDS_CONFIG)
        w = _pad_weights(w)
        logger.info(f"读取持久化基线: 权重={[round(x,2) for x in w]}, 阈值={t}")
        return w, dict(t)
    except Exception as e:
        logger.warning(f"读取基线文件失败: {e}，回退到初始值")
        return list(config.LOGIT_WEIGHTS), dict(config.DEFAULT_THRESHOLDS_CONFIG)


def save_baseline(weights: list[float], thresholds: dict[str, float], score: float):
    """持久化当前最优基线。"""
    path = config.OPTIMIZER_BASELINE_FILE
    data = {
        "weights": [round(w, 4) for w in weights],
        "thresholds": {k: round(v, 4) for k, v in thresholds.items()},
        "score": round(score, 4),
        "updated_at": datetime.now().isoformat(),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"基线已持久化: score={score:.4f}")


def write_back_to_config(weights: list[float], thresholds: dict[str, float]):
    """将更优的参数写回 config.py，实现永久性更新。"""
    cfg_path = os.path.join(
        os.path.dirname(__file__), "config.py"
    )

    with open(cfg_path) as f:
        content = f.read()

    # 替换 LOGIT_WEIGHTS — 用正则避免 4 位小数不匹配
    w_str = ", ".join(f"{w:.4f}" for w in weights)
    content = re.sub(
        r"LOGIT_WEIGHTS\s*=\s*\[.*?\]",
        f"LOGIT_WEIGHTS = [{w_str}]",
        content,
    )

    # 替换阈值
    for key in ("action", "alert", "watch"):
        new_val = thresholds.get(key, 0.0)
        content = re.sub(
            rf'"{key}"\s*:\s*[\d.]+',
            f'"{key}": {new_val}',
            content,
        )

    with open(cfg_path, "w") as f:
        f.write(content)

    logger.info(f"✅ config.py 已更新: 权重={[round(w,4) for w in weights]}, 阈值={thresholds}")


# =============================================================================
# CMA-ES 进化状态持久化
# =============================================================================

CMA_STATE_VERSION = config.CMA_ES_STATE_VERSION


def save_es_state(es: Any, n_generations: int, n_evals: int) -> None:
    """将 CMAEvolutionStrategy 内部状态 pickle 到磁盘。

    保存进化引擎的"记忆"——协方差矩阵 C、步长 σ、进化路径 pc、
    均值向量 mean、秩-μ更新矩阵等。下轮 resume 时恢复这些状态，
    分布会从上次中断处继续精炼，而非每 10 分钟从头开始。

    当基线权重变化时（apply_best 更新了 baseline），调用方会
    递增 CMA_STATE_VERSION，新旧版本不兼容的状态文件会被忽略。
    """
    path = config.CMA_ES_STATE_FILE
    try:
        data = {
            "state": pickle.dumps(es),
            "generations": n_generations,
            "evals": n_evals,
            "version": CMA_STATE_VERSION,
            "mean": [round(x, 4) for x in es.mean.tolist()],
            "sigma": round(es.sigma, 4),
            "updated_at": datetime.now().isoformat(),
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(data, f)
        logger.info(
            f"💾 CMA-ES 状态已持久化: σ={es.sigma:.4f}, "
            f"均值={[round(x,2) for x in es.mean.tolist()]}, "
            f"世代={n_generations}, v={CMA_STATE_VERSION}"
        )
    except Exception as e:
        logger.warning(f"保存 CMA-ES 状态失败: {e}")


def resume_es(x0: list[float], sigma0: float, opts: dict) -> tuple[Any | None, int, int]:
    """从磁盘恢复 CMAEvolutionStrategy 对象。

    Returns:
        (es, n_generations, n_evals) 或 (None, 0, 0) 表示无有效状态
    """
    import cma  # noqa: F811

    path = config.CMA_ES_STATE_FILE
    if not os.path.exists(path):
        return None, 0, 0

    try:
        with open(path, "rb") as f:
            data = pickle.load(f)

        # 版本检查：基线变化后旧状态无效
        saved_version = data.get("version", -1)
        if saved_version != CMA_STATE_VERSION:
            logger.info(
                f"CMA-ES 状态版本不匹配 (saved={saved_version} vs current={CMA_STATE_VERSION}), "
                f"忽略旧状态，从基线重新初始化"
            )
            return None, 0, 0

        es = pickle.loads(data["state"])
        n_gen = data.get("generations", 0)
        n_evals = data.get("evals", 0)

        logger.info(
            f"🔄 恢复 CMA-ES 状态: σ={es.sigma:.4f}, "
            f"均值={[round(x,2) for x in es.mean.tolist()]}, "
            f"已运行={n_gen}世代/{n_evals}次评估, v={saved_version}"
        )
        return es, n_gen, n_evals

    except Exception as e:
        logger.warning(f"恢复 CMA-ES 状态失败: {e}，从头开始")
        return None, 0, 0


def clear_es_state() -> None:
    """清除持久化的 CMA-ES 状态（基线变化时调用）。"""
    path = config.CMA_ES_STATE_FILE
    if os.path.exists(path):
        os.remove(path)
        logger.info("🧹 已清除 CMA-ES 状态缓存（基线变更）")


# =============================================================================
# Telegram 推送
# =============================================================================

def send_telegram_summary(compare: dict, improved: bool) -> str:
    """推送优化结果摘要到 Telegram，返回推送状态消息。"""
    baseline = compare.get("baseline", {})
    best = compare.get("best", {})
    imp = compare.get("improvement", {})

    ts = datetime.now().strftime("%m-%d %H:%M")

    if not improved:
        msg = (
            f"🤖 强赎策略优化 | {ts}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⚪ 未发现更优参数\n"
            f"基线 score={baseline.get('score', 0):.2f}\n"
            f"最优 score={best.get('score', 0):.2f}\n"
            f"({compare.get('n_iterations', 0)} 次迭代)"
        )
    else:
        # 权重变化标记
        old_w = [round(x, 2) for x in baseline.get("weights", [])]
        new_w = [round(x, 2) for x in best.get("weights", [])]
        changed = ["✅" if abs(a-b) > 0.1 else "──" for a, b in zip(old_w, new_w)]

        msg = (
            f"🎯 强赎策略优化 | {ts}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"基线 → 最优:\n"
            f"  胜率 {baseline.get('win_rate', 0)}% → {best.get('win_rate', 0)}%\n"
            f"  均收益 {baseline.get('avg_return', 0):+.2f}% → {best.get('avg_return', 0):+.2f}%\n"
            f"  总盈亏 ¥{baseline.get('total_pnl', 0):+.0f} → ¥{best.get('total_pnl', 0):+.0f}\n"
            f"\n"
            f"权重变化:\n"
        )
        for i, name in enumerate(config.LOGIT_WEIGHT_NAMES):
            msg += f"  {changed[i]} {name}: {old_w[i]} → {new_w[i]}\n"
        msg += (
            f"\n"
            f"score: {baseline.get('score', 0):.2f} → {best.get('score', 0):.2f}\n"
            f"({compare.get('n_iterations', 0)} 次迭代)"
        )

    # 发送 Telegram
    token_path = os.path.expanduser("~/.hermes/.env")
    token = ""
    if os.path.exists(token_path):
        for line in open(token_path):
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip("\"'")
                break

    if not token:
        return msg + "\n\n(无 TG token，仅本地输出)"

    import urllib.request
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
    }).encode()

    try:
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            return msg
    except Exception as e:
        logger.error(f"TG 推送失败: {e}")
        return msg + f"\n\n(TG 推送失败: {e})"


# =============================================================================
# 优化器（CMA-ES 引擎）
# =============================================================================

class StrategyOptimizer:
    """
    策略参数优化器（闭环版）。

    搜索策略：
    - CMA-ES：协方差自适应进化策略，自动学习参数相关性 + 自适应步长
    - 每个世代从协方差分布采样一批解，用 top 解更新分布
    - 找到更优参数后自动更新基线
    """

    def __init__(
        self,
        baseline_weights: list[float] | None = None,
        baseline_thresholds: dict[str, float] | None = None,
        score_fn: str = "balanced",
        hold_max_days: int = 15,
        target_exit_pct: float = 10.0,
        stop_loss_pct: float = -8.0,
        max_positions: int = 5,
        top_k: int = 10,
    ):
        # 从持久化文件加载基线
        loaded_w, loaded_t = load_baseline()
        self.baseline_weights = baseline_weights or loaded_w
        self.baseline_thresholds = baseline_thresholds or loaded_t

        self.score_fn = score_fn
        self.baseline_result: OptimizerResult | None = None
        self.results: list[OptimizerResult] = []

        # 回测参数
        self.hold_max_days = hold_max_days
        self.target_exit_pct = target_exit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_positions = max_positions
        self.top_k = top_k

        # CMA-ES 内部状态
        self._es = None  # cma.CMAEvolutionStrategy instance
        self._generation = 0
        self._snapshots = None

    def _clip_to_bounds(self, weights: list[float]) -> list[float]:
        """将权重剪裁到搜索空间范围内。"""
        return [
            max(WEIGHT_RANGES[j][0], min(WEIGHT_RANGES[j][1], weights[j]))
            for j in range(len(WEIGHT_RANGES))
        ]

    def _run_single_backtest(
        self, weights: list[float], thresholds: dict[str, float]
    ) -> tuple[dict, float]:
        """运行一次回测，返回 perf + score。"""
        engine = BacktestEngine(
            weights=weights,
            thresholds=thresholds,
            hold_max_days=self.hold_max_days,
            target_exit_pct=self.target_exit_pct,
            stop_loss_pct=self.stop_loss_pct,
            max_positions=self.max_positions,
            top_k=self.top_k,
        )
        trades = engine.run(snapshots=self._snapshots)
        perf = calc_performance(trades)
        score = self._calculate_score(perf)
        return perf, score

    def run_baseline(self, prebuilt_snapshots: pd.DataFrame | None = None) -> OptimizerResult:
        """运行当前基线回测。"""
        self._snapshots = prebuilt_snapshots
        logger.info("运行基线回测...")
        t0 = time.time()
        perf, score = self._run_single_backtest(
            self.baseline_weights, self.baseline_thresholds
        )
        elapsed = time.time() - t0

        self.baseline_result = OptimizerResult(
            weights=self.baseline_weights,
            thresholds=self.baseline_thresholds,
            total_trades=perf["total_trades"],
            win_rate=perf["win_rate"],
            avg_return=perf["avg_return"],
            total_pnl=perf["total_pnl"],
            max_return=perf["max_return"],
            min_return=perf["min_return"],
            score=score,
            elapsed=elapsed,
            timestamp=datetime.now().isoformat(),
        )
        return self.baseline_result

    def search(
        self,
        iterations: int = 50,
        seed: int | None = None,
        local_search_ratio: float = 0.8,
        prebuilt_snapshots: pd.DataFrame | None = None,
        resume: bool = False,
    ) -> list[OptimizerResult]:
        """
        用 CMA-ES 搜索参数空间。

        iterations 在这里被 reinterpreted 为"本轮总回测次数预算"。
        CMA-ES 按世代运行，每世代评估 popsize 个解。

        Args:
            iterations: 本轮总回测次数预算（≈ generations * popsize）
            prebuilt_snapshots: 预构建的快照DataFrame
            resume: True=从磁盘恢复上次进化状态继续搜索；
                    False=从基线权重重新初始化 CMA-ES（默认）
        """
        if self.baseline_result is None:
            self.run_baseline()

        self._snapshots = prebuilt_snapshots

        # 导入 CMA-ES
        try:
            import cma
        except ImportError:
            logger.error("CMA-ES 未安装，请运行: pip install cma")
            raise

        # 计算世代数
        n_generations = max(1, iterations // CMA_POPSIZE)
        logger.info(
            f"🚀 CMA-ES 搜索 | 预算={iterations}次回测, "
            f"种群={CMA_POPSIZE}, 世代={n_generations}, "
            f"seed={seed or 'random'}"
        )

        # 初始解：当前基线权重
        x0 = np.array(self.baseline_weights, dtype=float)
        sigma0 = 0.5  # 初始步长，权重约 ±50%

        # CMA-ES 选项
        bounds_low = [r[0] for r in WEIGHT_RANGES]
        bounds_high = [r[1] for r in WEIGHT_RANGES]

        opts = {
            "popsize": CMA_POPSIZE,
            "maxfevals": iterations,  # 最大函数评估次数（cma 会在超过后停掉）
            "bounds": [bounds_low, bounds_high],
            "verbose": -1,            # 不打印 CMA-ES 内部日志
            "CMA_diagonal": True,     # 前 2 代用对角CMA（更快适应），自动切换
            "tolfun": 1e-4,
            "tolx": 1e-4,
        }

        if seed is not None:
            opts["seed"] = seed

        # 尝试恢复历史状态（resume 模式）
        resumed = False
        if resume:
            es, prev_gen, prev_evals = resume_es(x0.tolist(), sigma0, opts)
            if es is not None:
                self._es = es
                eval_count = prev_evals
                generation = prev_gen
                resumed = True
                logger.info(
                    f"  从世代 #{prev_gen} (+{prev_evals}次评估) 处恢复，"
                    f"本轮预算 {iterations} 次"
                )

        if not resumed:
            self._es = cma.CMAEvolutionStrategy(x0, sigma0, opts)
            eval_count = 0
            generation = 0

        while not self._es.stop() and eval_count < iterations:
            generation += 1

            # 采样一批解
            solutions = self._es.ask()
            n_candidates = len(solutions)

            # 每个解用基线阈值评估一次（阈值在更新阶段单独优化）
            fitness = []
            for sol in solutions:
                weights = sol.tolist()
                weights = self._clip_to_bounds(weights)
                t0 = time.time()
                perf, score = self._run_single_backtest(weights, self.baseline_thresholds)
                eval_count += 1

                # CMA-ES 默认最小化，所以取负 score
                fitness.append(-score)

                # 记录结果
                self.results.append(OptimizerResult(
                    weights=weights,
                    thresholds=self.baseline_thresholds,
                    total_trades=perf["total_trades"],
                    win_rate=perf["win_rate"],
                    avg_return=perf["avg_return"],
                    total_pnl=perf["total_pnl"],
                    max_return=perf["max_return"],
                    min_return=perf["min_return"],
                    score=score,
                    elapsed=time.time() - t0,
                    timestamp=datetime.now().isoformat(),
                ))

            # 更新 CMA-ES 分布
            self._es.tell(solutions, fitness)

            # 进度日志
            best_now = self.best()
            logger.info(
                f"  世代 #{generation:2d} | "
                f"评估={eval_count:3d}/{iterations} | "
                f"best score={best_now.score:.2f} | "
                f"σ={self._es.sigma:.3f}"
            )

        logger.info(
            f"✅ CMA-ES 完成: {generation} 世代, {eval_count} 次评估, "
            f"终止原因: {self._es.stop()}"
        )

        # 持久化进化状态（供下轮 resume）
        save_es_state(self._es, generation, eval_count)

        self._generation = generation
        return self.sorted_results()

    def best(self) -> OptimizerResult | None:
        if not self.results:
            return None
        return max(self.results, key=lambda r: r.score)

    def sorted_results(self) -> list[OptimizerResult]:
        return sorted(self.results, key=lambda r: r.score, reverse=True)

    def is_improved(self, min_score_gain: float = 0.5) -> bool:
        """判断搜索是否找到明显更优的参数。"""
        best = self.best()
        if best is None or self.baseline_result is None:
            return False
        return best.score > self.baseline_result.score + min_score_gain

    def apply_best(self) -> bool:
        """应用最优参数：持久化 + 写回 config.py + 递增状态版本。"""
        best = self.best()
        if best is None:
            logger.warning("无最优结果可应用")
            return False

        # 持久化到文件
        save_baseline(best.weights, best.thresholds, best.score)

        # 写回 config.py
        write_back_to_config(best.weights, best.thresholds)

        # 基线变化 → 递增版本号 + 清除旧 CMA-ES 状态
        global CMA_STATE_VERSION
        CMA_STATE_VERSION += 1
        clear_es_state()
        logger.info(f"📌 CMA-ES 状态版本更新为 v{CMA_STATE_VERSION}（基线已变更）")

        return True

    def compare(self) -> dict[str, Any]:
        """返回基线 vs 最优的对比报告。"""
        best = self.best()
        if best is None or self.baseline_result is None:
            return {"error": "缺少基线或最优结果"}

        return {
            "baseline": asdict(self.baseline_result),
            "best": asdict(best),
            "improvement": {
                "score_pct": self._pct_change(self.baseline_result.score, best.score),
                "total_pnl_pct": self._pct_change(
                    self.baseline_result.total_pnl, best.total_pnl
                ),
                "win_rate_pct": best.win_rate - self.baseline_result.win_rate,
                "avg_return_pct": best.avg_return - self.baseline_result.avg_return,
            },
            "improved": bool(self.is_improved()),
            "n_iterations": len(self.results),
            "ranking": [asdict(r) for r in self.sorted_results()[:10]],
        }

    def _calculate_score(self, perf: dict) -> float:
        trades = perf["total_trades"]
        win_rate = perf["win_rate"]
        avg_return = perf["avg_return"]
        max_dd = abs(min(perf.get("min_return", 0), 0))

        if self.score_fn == "aggressive":
            return avg_return * 0.6 + win_rate * 0.2 - max_dd * 0.1 + trades * 0.1
        elif self.score_fn == "conservative":
            return win_rate * 0.5 - max_dd * 0.3 + avg_return * 0.2
        else:
            return win_rate * 0.4 + avg_return * 0.3 - max_dd * 0.2 + trades * 0.1

    @staticmethod
    def _pct_change(old: float, new: float) -> float:
        if old == 0:
            return 0.0
        return (new - old) / abs(old) * 100.0


# =============================================================================
# CLI 入口
# =============================================================================

def run_optimization(
    iterations: int = 50,
    score_mode: str = "balanced",
    push_telegram: bool = False,
    verbose: bool = True,
    apply_if_improved: bool = True,
    resume: bool = False,
    hold_max_days: int = 15,
    target_exit_pct: float = 10.0,
    stop_loss_pct: float = -8.0,
    max_positions: int = 5,
    top_k: int = 5,
) -> dict[str, Any]:
    """运行一次完整的优化周期。"""
    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

    logger.info(f"🚀 CMA-ES 优化周期 | 预算={iterations}次回测, score={score_mode}, "
                f"push={push_telegram}, resume={'✅' if resume else '⚪'}")

    # 预构建快照，所有搜索迭代复用
    from strategies.cb_redemption.data import build_historical_snapshots, clear_cache
    clear_cache()
    logger.info("预构建历史快照（近3年）...")
    snapshots = build_historical_snapshots(start="20230101")
    logger.info(f"快照预构建完成: {len(snapshots)} 行, {snapshots['date'].nunique()} 交易日")

    optimizer = StrategyOptimizer(score_fn=score_mode,
                                  hold_max_days=hold_max_days, target_exit_pct=target_exit_pct,
                                  stop_loss_pct=stop_loss_pct, max_positions=max_positions,
                                  top_k=top_k)

    # 基线
    baseline = optimizer.run_baseline(prebuilt_snapshots=snapshots)
    logger.info(f"基线: trades={baseline.total_trades}, "
                f"胜率={baseline.win_rate}%, "
                f"收益={baseline.avg_return:+.2f}%, "
                f"score={baseline.score:.2f}")

    # CMA-ES 搜索
    t0 = time.time()
    optimizer.search(iterations=iterations, prebuilt_snapshots=snapshots, resume=resume)
    search_elapsed = time.time() - t0
    compare = optimizer.compare()
    best = optimizer.best()

    # 评估改进
    improved = optimizer.is_improved()

    # 自动应用
    if improved and apply_if_improved:
        optimizer.apply_best()
        logger.info(f"✅ 发现更优参数，已更新基线!")
    elif not improved and verbose:
        logger.info(f"⚪ 未发现优于基线的参数 (best score={best.score:.2f} vs baseline={baseline.score:.2f})")

    # Telegram 推送
    if push_telegram:
        msg = send_telegram_summary(compare, improved)
        logger.info(f"TG 推送完成")

    # 控制台输出
    if verbose:
        print(f"\n{'='*50}")
        print(f"📊 CMA-ES 优化结果")
        print(f"{'='*50}")
        print(f"耗时: {search_elapsed:.1f}s ({optimizer._generation} 世代, {len(optimizer.results)} 次回测)")
        print(f"改进: {'✅ 是' if improved else '⚪ 否'}")
        print()

        print(f"基线:")
        w = [round(x, 2) for x in baseline.weights]
        print(f"  权重: {w}")
        print(f"  阈值: {baseline.thresholds}")
        print(f"  score={baseline.score:.2f} | 胜率={baseline.win_rate}% | 收益={baseline.avg_return:+.2f}%")
        print()

        print(f"最优:")
        w = [round(x, 2) for x in best.weights]
        print(f"  权重: {w}")
        print(f"  阈值: {best.thresholds}")
        print(f"  score={best.score:.2f} | 胜率={best.win_rate}% | 收益={best.avg_return:+.2f}%")
        print()

        if improved:
            print(f"改进幅度:")
            for k, v in compare["improvement"].items():
                print(f"  {k}: {v:+.2f}")
            print()

        print(f"Top 3:")
        for i, r in enumerate(compare["ranking"][:3]):
            w = [round(x, 2) for x in r["weights"]]
            print(f"  #{i+1}: score={r['score']:.2f} | 权重{w} | "
                  f"胜率={r['win_rate']}% | 收益={r['avg_return']:+.2f}% | 交易={r['total_trades']}")

        print(f"{'='*50}")

    return compare


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="可转债强赎策略参数优化器（CMA-ES 引擎）")
    parser.add_argument("--iterations", type=int, default=50,
                        help="总回测预算次数（默认 50，≈ 5 世代 × 10 种群）")
    parser.add_argument("--score", choices=["balanced", "aggressive", "conservative"],
                        default="balanced", help="评分模式")
    parser.add_argument("--push", action="store_true", help="推送到 Telegram")
    parser.add_argument("--no-apply", action="store_true", help="不自动更新基线")
    parser.add_argument("--resume", action="store_true", help="从上次 CMA-ES 进化状态继续搜索（而非重新初始化）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--hold_max_days", type=int, default=15, help="最大持仓天数")
    parser.add_argument("--target_exit_pct", type=float, default=10.0, help="止盈百分比")
    parser.add_argument("--stop_loss_pct", type=float, default=-8.0, help="止损百分比")
    parser.add_argument("--max_positions", type=int, default=5, help="最大持仓数量")
    parser.add_argument("--top_k", type=int, default=5, help="每日候选池大小")
    args = parser.parse_args()

    result = run_optimization(
        iterations=args.iterations,
        score_mode=args.score,
        push_telegram=args.push,
        verbose=not args.json,
        apply_if_improved=not args.no_apply,
        resume=args.resume,
        hold_max_days=args.hold_max_days,
        target_exit_pct=args.target_exit_pct,
        stop_loss_pct=args.stop_loss_pct,
        max_positions=args.max_positions,
        top_k=args.top_k,
    )

    if args.json:
        class _NpEncoder(json.JSONEncoder):
            def default(self, obj):
                import numpy as np
                if isinstance(obj, (np.integer,)): return int(obj)
                if isinstance(obj, (np.floating,)): return float(obj)
                if isinstance(obj, (np.bool_,)): return bool(obj)
                if isinstance(obj, np.ndarray): return obj.tolist()
                return super().default(obj)
        print(json.dumps(result, ensure_ascii=False, indent=2, cls=_NpEncoder))
