from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] == "gui":
        if arguments and arguments[0] == "gui":
            arguments.pop(0)
        from synthrecall.app import main as gui_main

        return gui_main([sys.argv[0], *arguments])

    from synthrecall.cli import main as cli_main

    return cli_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
