# Run Manifest YAML Schema (F2 spec)

**版本**: schema_version=1
**建立**: 2026-05-16 framework deep review Round 4 Codex 批准
**目的**: 解决 framework debate 暴露的 C1 (artifact identity 弱) — 每次回测产物 cite 文件夹 + metric 不够, 必须 stable run manifest 给 commit / dirty-file / config hash / data snapshot / cost / promotion 等

## 触发条件

任何回测或评估 batch 完成时必须**同 handoff** 产出 manifest yaml. 不触发: 单纯 ACK / recon / schema check / 算力估算.

## 存放位置

`data/research_framework/run_manifests/<batch_id>.yaml`

`batch_id` 格式: `<strategy_id>_<hypothesis_id>_<YYYY-MM-DD>_<HHMM>`

例:
- `cb_arb_panic-detector-breadth-v1_2026-05-15_1147.yaml`
- `cb_arb_main-strategy-baseline_2026-05-15_2240.yaml`

## 完整 Schema

```yaml
schema_version: 1
batch_id: <strategy_id>_<hypothesis_id>_<YYYY-MM-DD>_<HHMM>
strategy_id: cb_arb  # 必填, 跟 CURRENT.md strategy_id 一致
hypothesis_id: panic-detector-breadth-v1  # 必填, 一句话假设的 slug

data_window:
  start: 2019-01-01
  end: 2024-12-31

# === 复现性 ===
config_path: scripts/evaluate_cb_arb_market_breadth_panic.py  # 必填
config_hash: <md5 of relevant params/config>  # 必填
entrypoint: python scripts/evaluate_cb_arb_market_breadth_panic.py --grid ...  # 必填, 完整命令
git_commit: abc1234  # 必填, 7 char hash, HEAD at run time
git_dirty:  # 必填, 列 git status --short 输出 (修改 + untracked)
  - scripts/evaluate_cb_arb_market_breadth_panic.py  # untracked
  - strategies/cb_arb/verifier.py  # modified
dirty_policy: allowed_with_list  # 必填: allowed_with_list | forbidden | unknown
data_snapshot:  # 必填, 每个使用的关键数据文件
  cb_daily.parquet:
    md5: <hash>
    rows: 543589
  cb_basic.parquet:
    md5: <hash>
    rows: 872

# === 算力 ===
compute_host: tencent-spot ins-5lb9zo12 (16 vCPU SPOTPAID)  # 必填
compute_cost_yuan: 3.2  # 必填 (sig 零边际填 0)
start_at: 2026-05-15T11:47:00Z  # 必填 UTC
end_at: 2026-05-15T12:00:00Z  # 必填 UTC
exit_code: 0  # 必填

# === 结果 ===
result_artifact: data/cb_arb_market_breadth_panic_2026-05-15/  # 必填, 文件夹路径
artifact_hash: <md5 of artifact_hash_manifest.txt>  # 必填; 多文件用 manifest hash
artifact_hash_manifest: data/cb_arb_market_breadth_panic_2026-05-15/artifact_hash_manifest.txt  # 必填若 artifact 是文件夹, 该文件含每个 sub-file 的 md5
result_summary: 0/162 过 4 floor, CV 3/6 holdout, reject  # 必填, 一句话结论

# === 决策 ===
promotion_status: rejected  # 必填: experiment | wip | adopted | rejected | archived | stale | invalidated
reviewer: claude  # 必填: claude | codex | user | auto
verdict_at: 2026-05-15T14:30:00Z  # 必填 UTC, 决策时间

# === Self ===
manifest_hash: null  # finalization 时填 (manifest 自己写完后 hash 自己, 防止后续被改)
```

## 字段说明

### `dirty_policy` 三态

- `allowed_with_list`: 跑前知道有 dirty file, 已审, 接受 (典型: WIP 研究分支)
- `forbidden`: 跑前 hard fail (典型: 生产 baseline 必须 clean)
- `unknown`: 没审, 后续要补判 (manifest validator warn)

### `artifact_hash` 多文件

- 单 file artifact: 直接填 file md5
- 多 file 文件夹: 填 `artifact_hash_manifest.txt` 自己的 md5; `artifact_hash_manifest.txt` 内容例:
  ```
  <md5>  ranked.csv
  <md5>  summary.json
  <md5>  daily_equity.csv
  <md5>  trades.csv
  ```

### `promotion_status` (8 status, see protocol U16 state transitions)

- `experiment`: 临时尝试, 不进 baseline_registry
- `wip`: 研究中, 进 baseline_registry
- `adopted`: 通过决策契约, 可投实盘 (需用户拍板)
- `rejected`: 触发任一 kill 条件
- `archived`: 用户手动决定永久归档
- `stale`: 90 天没复跑 / 代码 commit 改 / schema 变, 需 revalidate
- `invalidated`: 复跑发现错, 主动撤回

## 跟其他 spec 关系

- F1 (CURRENT.md 决策契约): manifest 必须跟 CURRENT.md 该策略段的 baseline_row 对应
- F3 (baseline_registry 加 manifest_path 列): 每个 baseline_registry 行的 manifest_path 字段指向这里
- F4 (state transitions): manifest 的 promotion_status 字段对应 U16 协议 8 state
- F5 (validate_run_manifest.py): warn-only validator 检查 parse + 必填字段

## 创建 workflow

1. Codex/Claude 跑 batch (回测/评估), 完成
2. 同 handoff 写 `data/research_framework/run_manifests/<batch_id>.yaml`
3. `manifest_hash` 留 null (写完所有字段后, finalization 时填 sha256 of file content sans this line)
4. CURRENT.md 该策略段 + baseline_registry 加新行 (链 manifest_path)
5. RESPONSE 提到 manifest_path + batch_id
6. Validator (F5) 检查 — warn 缺字段, fail YAML parse error

## 历史回填 (phase 1)

历史 baseline 已存的, manifest_path 字段先填 null, 后续如复跑则补完整 manifest.
