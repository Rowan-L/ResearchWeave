from pathlib import Path
import multiprocessing as mp
import os
import sys
import traceback


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

def _main() -> int:
    os.chdir(ROOT)
    from synthrecall.__main__ import main

    return main(sys.argv[1:])


def _write_startup_error() -> Path | None:
    try:
        log_path = ROOT / "data" / "startup_error.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        return log_path
    except OSError:
        return None


if __name__ == "__main__":
    mp.freeze_support()
    try:
        raise SystemExit(_main())
    except SystemExit:
        raise
    except Exception:
        log_path = _write_startup_error()
        if log_path is not None:
            print(f"ResearchWeave 启动失败，错误日志：{log_path}", file=sys.stderr)
        raise
