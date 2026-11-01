from pathlib import Path
import multiprocessing as mp
import sys


ROOT = Path(__file__).resolve().parent
BABELDOC_SRC = ROOT / "BabelDOC-main"

if BABELDOC_SRC.exists() and str(BABELDOC_SRC) not in sys.path:
    sys.path.insert(0, str(BABELDOC_SRC))


def main() -> int:
    if sys.platform == "win32":
        try:
            mp.set_start_method("spawn")
        except RuntimeError:
            pass

    from babeldoc.main import cli

    cli()
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
