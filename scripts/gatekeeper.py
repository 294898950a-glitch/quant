"""GateKeeper — 研究每个阶段的强制检查入口 (面向对象封装).

每个跑批/分析脚本启动前调对应方法, 不合规直接 sys.exit(1) 拒跑, 不等到 git
commit 才暴露问题. 避免"跑完才知道 spec 不合规, 算力白烧".

Usage:
    from scripts.gatekeeper import GateKeeper

    gate = GateKeeper()
    gate.before_run_grid(Path("data/cb_arb_xxx/spec.yaml"))
    # ... 跑回测
    gate.after_run_grid(Path("data/cb_arb_xxx/"))
    # ... Claude 写 l4_ack 判断
    gate.before_l5_diagnostic(Path("data/cb_arb_xxx/"))
    # ... 更新 CURRENT 前
    gate.before_commit_truth(Path("data/cb_arb_xxx/"))

设计哲学:
- fail-fast: 不过就 exit, 不返回 False 让 caller 决定
- 单一 entry: 每个研究阶段一个方法名, caller 不需要知道内部跑哪些 validator
- 闭包: 一个对象包所有阶段
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


class GateKeeperError(SystemExit):
    pass


class GateKeeper:
    """研究每个阶段的强制检查入口."""

    def __init__(self, repo_root: Path | None = None, quiet: bool = False):
        self.repo_root = repo_root or REPO_ROOT
        self.scripts = self.repo_root / "scripts"
        self.quiet = quiet

    # === 公开方法 (按研究阶段) ===

    def before_run_grid(self, spec_path: Path) -> None:
        """跑回测前: spec.yaml 合规 + 数据 schema OK + 预算配置 OK + sanity 检查."""
        self._log(f"[GateKeeper] before_run_grid: {spec_path}")
        self._must_run("validate_spec.py", [str(spec_path)],
                       fail_msg="spec.yaml 不合规, 拒跑回测")
        self._must_run("validate_data_schema.py", [],
                       fail_msg="数据 warehouse schema 不全, 拒跑")
        self._must_run("validate_compute_budget.py", [],
                       fail_msg="预算配置文件损坏, 拒跑")
        # 按 Codex framework holistic review Q2-A: sanity_checker 升级 yaml 后接入
        # (commit 升级 sanity_checker yaml schema 同 commit 接入). 检查 spec 字段
        # 语义合理性 (range / hard_floors scale / 路径 / cv years / budget vs cost).
        self._must_run("research_sanity_checker.py", ["--spec", str(spec_path)],
                       fail_msg="spec semantic 检查失败 (range/hard_floors/路径/budget), 拒跑")
        self._log("  ✓ 启动 grid 检查全过")

    def after_run_grid(self, run_dir: Path | None = None) -> None:
        """跑完回测: run_manifest 完整 + 自动算 L4 数据.

        注: 当前 validate_run_manifest + auto_compute_l4_data 都扫所有 active run,
        run_dir 参数仅用于 logging context. 未来 validator 支持 single-run 时改传.
        按 Codex 12:07 review: 参数保留 (信息性), 但语义说清.
        """
        self._log(f"[GateKeeper] after_run_grid (context: {run_dir})")
        self._must_run("validate_run_manifest.py", [],
                       fail_msg="run_manifest.yaml 不全, 拒进入 L4")
        self._must_run("auto_compute_l4_data.py", [],
                       fail_msg="L4 数据自动算失败, 检查 ranked.csv / trades.csv")
        self._log("  ✓ 跑批产物 + L4 数据自动算 完整")

    def before_l5_diagnostic(self, run_dir: Path | None = None) -> None:
        """L5 反向诊断前: Claude 已写 L4 判断, 校验合规.

        run_dir 同 after_run_grid, 仅 logging context. validator 内部扫所有
        RUNNING/COMPLETE 状态 run.
        """
        self._log(f"[GateKeeper] before_l5_diagnostic (context: {run_dir})")
        self._must_run("validate_l4_ack.py", [],
                       fail_msg="L4 ack 不全 (Claude 没填判断 / 数据没自动算), 拒进 L5")
        # 按 Codex framework Q1 P1: L5 反向诊断 yaml schema 强制
        # (mini-spec-retry/reject 时 diagnostic.yaml 必有结构化 root cause)
        self._must_run("validate_l5_diagnostic.py", [],
                       fail_msg="L5 diagnostic.yaml 缺失或不合规 (retry/reject 时必填)")
        self._log("  ✓ L4 ack 完整 + L5 diagnostic 合规, 可以跑 L5")

    def before_commit_truth(self, run_dir: Path | None = None) -> None:
        """commit CURRENT / baseline_registry 前: 完整 preflight."""
        self._log(f"[GateKeeper] before_commit_truth")
        self._must_run("framework_preflight.py", ["--quiet"],
                       fail_msg="framework_preflight 失败, 不能更新真值")
        self._log("  ✓ 真值更新 OK")

    def quick_check(self) -> None:
        """一次性校验所有, 给 CI 或 manual 用."""
        self._log("[GateKeeper] quick_check (all-in-one)")
        self._must_run("framework_preflight.py", ["--quiet"],
                       fail_msg="quick_check 失败")
        self._log("  ✓ all clear")

    # === 内部方法 ===

    # Whitelist: 仅这些 validator 用 exit 2 表示 warning-only. 其他脚本非 0 都当 fail
    # (避免 Python error / missing script 也被放行).
    WARN_ONLY_WHITELIST = {"validate_data_schema.py", "framework_preflight.py"}

    def _must_run(self, script: str, args: list[str], fail_msg: str) -> None:
        """exit code 处理:
        - 0 = OK
        - 1 = strict fail (拦)
        - 2 = warning-only (只放行 WARN_ONLY_WHITELIST 里的脚本; 其他脚本 2 也是 fail)
        - 其他非 0 (Python error / missing script / 等) = fail
        """
        cmd = [sys.executable, str(self.scripts / script)] + args
        result = subprocess.run(cmd, cwd=self.repo_root)
        rc = result.returncode
        if rc == 0:
            return
        if rc == 2 and script in self.WARN_ONLY_WHITELIST:
            self._log(f"  ⚠ WARN-ONLY: {script} returned warnings, GateKeeper 放行")
            return
        self._log(f"  ✗ FAIL ({script} exit {rc}): {fail_msg}")
        raise GateKeeperError(1)

    def _log(self, msg: str) -> None:
        if not self.quiet:
            print(msg, file=sys.stderr)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="GateKeeper CLI")
    parser.add_argument("stage", choices=[
        "before_run_grid", "after_run_grid", "before_l5_diagnostic",
        "before_commit_truth", "quick_check",
    ])
    parser.add_argument("path", nargs="?")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    gate = GateKeeper(quiet=args.quiet)
    if args.stage == "before_run_grid":
        if not args.path:
            sys.exit("ERROR: before_run_grid requires spec.yaml path")
        gate.before_run_grid(Path(args.path))
    elif args.stage == "after_run_grid":
        if not args.path:
            sys.exit("ERROR: after_run_grid requires run_dir path")
        gate.after_run_grid(Path(args.path))
    elif args.stage == "before_l5_diagnostic":
        gate.before_l5_diagnostic(Path(args.path) if args.path else None)
    elif args.stage == "before_commit_truth":
        gate.before_commit_truth(Path(args.path) if args.path else None)
    elif args.stage == "quick_check":
        gate.quick_check()
    sys.exit(0)
