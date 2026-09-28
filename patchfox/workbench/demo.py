"""Offline acceptance fixture: scripted model, real PatchFox engine and tools."""

import argparse
import json
from pathlib import Path

from .artifacts import git

DEMO_PROMPT = "修复 pricing.py：满 100 元（含 100）免运费，否则收取 10 元。不要修改测试，完成后说明修改。"
TESTS = """import unittest
from pricing import shipping_fee

class ShippingTests(unittest.TestCase):
    def test_below_threshold(self):
        self.assertEqual(shipping_fee(99), 10)

    def test_at_threshold(self):
        self.assertEqual(shipping_fee(100), 0)

    def test_above_threshold(self):
        self.assertEqual(shipping_fee(101), 0)

if __name__ == "__main__":
    unittest.main()
"""


def create_demo(root):
    root = Path(root).resolve()
    if root.exists():
        # Never silently overwrite an existing demo or somebody's work.
        if not (root / ".git").is_dir():
            raise ValueError("演示目录已存在但不是 Git 仓库：" + str(root))
        return root
    root.mkdir(parents=True)
    (root / "pricing.py").write_text(
        "def shipping_fee(total):\n    return 0 if total > 100 else 10\n",
        encoding="utf-8",
    )
    (root / "test_pricing.py").write_text(TESTS, encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n.patchfox/\n", encoding="utf-8")
    git(root, "init")
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=PatchFox Demo",
        "-c",
        "user.email=demo@localhost",
        "commit",
        "-m",
        "Demo: shipping threshold with a failing boundary test",
    )
    return root


def main():
    from patchfox import PatchFox, SessionStore, WorkspaceContext
    from patchfox.testing import ScriptedModelClient

    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--max-steps", type=int, default=10)
    args = parser.parse_args()

    def tool(name, **kwargs):
        return "<tool>" + json.dumps({"name": name, "args": kwargs}) + "</tool>"

    client = ScriptedModelClient(
        [
            tool("read_file", path="pricing.py"),
            tool(
                "patch_file",
                path="pricing.py",
                old_text="total > 100",
                new_text="total >= 100",
            ),
            "<final>已将免运费条件改为 total >= 100；独立验收由工作台运行。此为脚本模型演示。</final>",
        ]
    )
    agent = PatchFox(
        model_client=client,
        workspace=WorkspaceContext.build(args.cwd),
        session_store=SessionStore(Path(args.cwd) / ".patchfox" / "sessions"),
        session={"id": args.session_id, "history": []},
        approval_policy="auto",
        max_steps=args.max_steps,
        auto_dream=False,
        allowed_tools=["read_file", "patch_file"],
    )
    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    print("OFFLINE DEMO — scripted model; real Runtime, file tools and evidence.")
    print(agent.ask(prompt))


if __name__ == "__main__":
    main()
