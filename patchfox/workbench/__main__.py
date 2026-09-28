"""Run with: python -m patchfox.workbench --demo."""

import argparse
import shlex
from pathlib import Path

from .demo import create_demo
from .service import Settings


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="PatchFox 本地任务工作台（单用户、单执行槽）"
    )
    parser.add_argument(
        "--repo", type=Path, action="append", help="可提交任务的 Git 仓库根目录，可重复"
    )
    parser.add_argument(
        "--data-dir", type=Path, default=Path.home() / ".patchfox" / "workbench"
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--demo", action="store_true", help="离线脚本模型演示，不调用模型 API"
    )
    parser.add_argument(
        "--test-command",
        default="",
        help='独立验收命令，例如 "python -m pytest -q"；不经 shell',
    )
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument(
        "--timeout", type=int, default=900, help="单次 Agent 最长运行秒数"
    )
    args = parser.parse_args(argv)
    if args.timeout < 1 or not 1 <= args.port <= 65535:
        parser.error("timeout 必须为正数，port 必须为 1–65535")
    data_dir = args.data_dir.expanduser().resolve()
    if args.demo and args.repo:
        parser.error("--demo 使用专用样例仓库，不能同时指定 --repo")
    repositories = (
        [create_demo(data_dir / "demo-repo")]
        if args.demo
        else [path.expanduser().resolve() for path in (args.repo or [Path.cwd()])]
    )
    try:
        test_argv = (
            ["python", "-m", "unittest", "-v"]
            if args.demo
            else shlex.split(args.test_command)
        )
    except ValueError as exc:
        parser.error(str(exc))
    if not args.demo and any(data_dir.is_relative_to(repo) for repo in repositories):
        parser.error("--data-dir 必须位于源仓库之外")
    try:
        import uvicorn

        from .app import create_app
    except ImportError:
        parser.error('请先安装工作台依赖：python -m pip install -e ".[workbench]"')
    settings = Settings(
        data_dir,
        repositories,
        test_argv,
        args.demo,
        args.provider,
        args.model,
        args.timeout,
    )
    print(f"PatchFox Workbench: http://127.0.0.1:{args.port}")
    print(f"数据目录: {data_dir}")
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
