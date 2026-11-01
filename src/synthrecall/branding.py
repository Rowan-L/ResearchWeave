from __future__ import annotations

from pathlib import Path

APP_NAME = "ResearchWeave"
APP_NAME_ZH = "研脉"
APP_DISPLAY_NAME = f"{APP_NAME} · {APP_NAME_ZH}"


def app_icon_path(*, ico: bool = False) -> Path:
    suffix = "ico" if ico else "png"
    return Path(__file__).resolve().parent / "assets" / f"researchweave_icon.{suffix}"
