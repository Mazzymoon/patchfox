from __future__ import annotations

import sys

from patchfox.cli import build_agent, build_arg_parser, prepare_configuration
from patchfox.tui.app import PatchFoxTuiApp


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.prompt:
        print("patchfox-tui does not accept one-shot prompts; start the TUI and type there.", file=sys.stderr)
        return 2
    try:
        warnings = prepare_configuration(args, "tui")
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        agent = build_agent(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    PatchFoxTuiApp(agent).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
