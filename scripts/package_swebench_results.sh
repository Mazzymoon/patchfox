#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   bash scripts/package_swebench_results.sh \
#     artifacts/swebench_experiments/p1-random-seed42
#
# Optional full mode:
#   bash scripts/package_swebench_results.sh \
#     artifacts/swebench_experiments/p1-random-seed42 \
#     --full
#
# Default:
#   打包实验分析真正需要的数据，不包含 SWE-bench 官方 harness
#   中体积较大的完整 evaluation logs。
#
# --full:
#   额外包含 evaluation/logs，适合需要详细排查官方评分失败时使用。


if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <experiment_dir> [--full]"
    exit 1
fi

EXPERIMENT_DIR="$(realpath "$1")"
MODE="${2:-}"

if [[ ! -d "$EXPERIMENT_DIR" ]]; then
    echo "ERROR: experiment directory not found:"
    echo "  $EXPERIMENT_DIR"
    exit 1
fi

EXPERIMENT_ID="$(basename "$EXPERIMENT_DIR")"

PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
BUNDLE_ROOT="$PROJECT_ROOT/artifacts/result_bundles"

TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
BUNDLE_NAME="${EXPERIMENT_ID}_${TIMESTAMP}"
STAGING_DIR="$BUNDLE_ROOT/$BUNDLE_NAME"
ARCHIVE_PATH="$BUNDLE_ROOT/${BUNDLE_NAME}.tar.gz"

mkdir -p "$STAGING_DIR"


copy_file() {
    local src="$1"
    local dst="$2"

    if [[ -f "$src" ]]; then
        mkdir -p "$(dirname "$dst")"
        cp -p "$src" "$dst"
    fi
}


echo "========================================"
echo "Packaging SWE-bench experiment"
echo "experiment : $EXPERIMENT_ID"
echo "source     : $EXPERIMENT_DIR"
echo "mode       : ${MODE:-core}"
echo "========================================"


# ============================================================
# 1. 实验级核心结果
# ============================================================

CORE_FILES=(
    "experiment_config.json"
    "experiment_summary.json"
    "experiment_summary.md"
    "per_instance_results.csv"
    "predictions.jsonl"
    "progress.json"
    "selection_order.json"
)

for rel in "${CORE_FILES[@]}"; do
    copy_file \
        "$EXPERIMENT_DIR/$rel" \
        "$STAGING_DIR/$rel"
done


# ============================================================
# 2. 官方 SWE-bench evaluation 汇总
# ============================================================

EVAL_FILES=(
    "official_results.json"
    "official_eval.log"
    "harness_run.json"
)

for rel in "${EVAL_FILES[@]}"; do
    copy_file \
        "$EXPERIMENT_DIR/evaluation/$rel" \
        "$STAGING_DIR/evaluation/$rel"
done

# evaluation 根目录下可能存在：
# deepseek-v4-flash.<experiment>.json
find "$EXPERIMENT_DIR/evaluation" \
    -maxdepth 1 \
    -type f \
    -name '*.json' \
    -print0 2>/dev/null |
while IFS= read -r -d '' file; do
    name="$(basename "$file")"
    copy_file \
        "$file" \
        "$STAGING_DIR/evaluation/$name"
done


# ============================================================
# 3. Generation 结果
#
# 这些是后续分析最重要的数据：
# - patch
# - prediction
# - metadata
# - Agent trace
# - task state
# - stdout/stderr
# ============================================================

GEN_ROOT="$EXPERIMENT_DIR/generation"

if [[ -d "$GEN_ROOT" ]]; then

    copy_file \
        "$GEN_ROOT/predictions.jsonl" \
        "$STAGING_DIR/generation/predictions.jsonl"

    for instance_dir in "$GEN_ROOT"/*; do
        [[ -d "$instance_dir" ]] || continue

        instance="$(basename "$instance_dir")"
        dst="$STAGING_DIR/generation/$instance"

        INSTANCE_FILES=(
            "metadata.json"
            "patch.diff"
            "prediction.json"
            "stdout.log"
            "stderr.log"
        )

        for rel in "${INSTANCE_FILES[@]}"; do
            copy_file \
                "$instance_dir/$rel" \
                "$dst/$rel"
        done

        EVIDENCE_FILES=(
            "report.json"
            "session.json"
            "task_state.json"
            "trace.jsonl"
            "session.events.jsonl"
        )

        for rel in "${EVIDENCE_FILES[@]}"; do
            copy_file \
                "$instance_dir/evidence/$rel" \
                "$dst/evidence/$rel"
        done
    done
fi


# ============================================================
# 4. 官方 evaluator 每实例关键结果
#
# 默认只保存：
# report.json
# patch.diff
#
# full 模式额外保存：
# run_instance.log
# test_output.txt
# eval.sh
# ============================================================

EVAL_LOG_ROOT="$EXPERIMENT_DIR/evaluation/logs/run_evaluation"

if [[ -d "$EVAL_LOG_ROOT" ]]; then

    find "$EVAL_LOG_ROOT" -type f \
        \( -name 'report.json' -o -name 'patch.diff' \) \
        -print0 |
    while IFS= read -r -d '' file; do
        rel="${file#"$EXPERIMENT_DIR/"}"
        copy_file "$file" "$STAGING_DIR/$rel"
    done

    if [[ "$MODE" == "--full" ]]; then
        echo "Including full evaluation logs..."

        find "$EVAL_LOG_ROOT" -type f \
            \( \
                -name 'run_instance.log' \
                -o -name 'test_output.txt' \
                -o -name 'eval.sh' \
                -o -name 'run.json' \
            \) \
            -print0 |
        while IFS= read -r -d '' file; do
            rel="${file#"$EXPERIMENT_DIR/"}"
            copy_file "$file" "$STAGING_DIR/$rel"
        done
    fi
fi


# ============================================================
# 5. 保存代码版本信息，保证以后知道这次实验用了哪版代码
# ============================================================

{
    echo "experiment_id=$EXPERIMENT_ID"
    echo "packaged_at=$(date --iso-8601=seconds)"
    echo "hostname=$(hostname)"
    echo

    if git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "git_commit=$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
        echo "git_branch=$(git -C "$PROJECT_ROOT" branch --show-current)"
        echo
        echo "[git status]"
        git -C "$PROJECT_ROOT" status --short || true
    fi
} > "$STAGING_DIR/BUNDLE_INFO.txt"


# ============================================================
# 6. 文件清单 + SHA256
# ============================================================

(
    cd "$STAGING_DIR"

    find . -type f \
        ! -name 'SHA256SUMS.txt' \
        -print0 \
        | sort -z \
        | xargs -0 sha256sum
) > "$STAGING_DIR/SHA256SUMS.txt"


# ============================================================
# 7. 压缩
# ============================================================

mkdir -p "$BUNDLE_ROOT"

tar \
    -C "$BUNDLE_ROOT" \
    -czf "$ARCHIVE_PATH" \
    "$BUNDLE_NAME"


# 删除临时目录，只留 tar.gz
rm -rf "$STAGING_DIR"


echo
echo "========================================"
echo "Bundle created successfully"
echo "========================================"
echo
echo "Archive:"
echo "  $ARCHIVE_PATH"
echo
echo "Size:"
du -h "$ARCHIVE_PATH"
echo
echo "Copy back to local machine with scp."