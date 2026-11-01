from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz

from synthrecall.settings import LLMSettings


@dataclass(frozen=True)
class PDFTranslationRequest:
    input_pdf: Path
    output_dir: Path
    settings: LLMSettings
    api_key: str
    lang_in: str = "en"
    lang_out: str = "zh"
    qps: int = 2
    # F20-3:默认只输出译文单本(--no-dual);双语对照体积翻倍且多数人
    # 不看,需要时在设置里打开「同时生成双语对照」。
    dual_output: bool = False
    # B14:由调用方显式传入,避免依赖 output_dir 的层级假设;
    # 缺省时回退旧推导(output_dir 的上两级),兼容既有调用。
    babeldoc_home: Path | None = None
    app_root: Path | None = None
    cancel_requested: Callable[[], bool] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class PDFTranslationResult:
    mono_pdf: Path | None
    dual_pdf: Path | None
    output_dir: Path
    log_tail: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tokens_estimated: bool = False


class TranslationCancelledError(RuntimeError):
    pass


def translate_pdf_with_babeldoc(request: PDFTranslationRequest) -> PDFTranslationResult:
    input_pdf = request.input_pdf.resolve()
    if not input_pdf.exists():
        raise FileNotFoundError(f"PDF 文件不存在：{input_pdf}")
    if not request.api_key.strip():
        raise ValueError("请先在 API 设置中填写 API Key。")

    output_dir = request.output_dir.resolve()
    working_dir = output_dir / "_working"
    output_dir.mkdir(parents=True, exist_ok=True)
    working_dir.mkdir(parents=True, exist_ok=True)

    command = [
        *_babeldoc_command_prefix(),
        "--openai",
        "--openai-model",
        request.settings.model,
        "--openai-base-url",
        request.settings.base_url,
        "--openai-api-key-header",
        request.settings.api_auth_style,
        "--lang-in",
        request.lang_in,
        "--lang-out",
        request.lang_out,
        "--qps",
        str(request.qps),
        "--pool-max-workers",
        str(request.qps),
        "--no-auto-extract-glossary",
        "--watermark-output-mode",
        "no_watermark",
        "--output",
        str(output_dir),
        "--working-dir",
        str(working_dir),
        "--files",
        str(input_pdf),
    ]
    if not request.settings.send_temperature:
        command.append("--no-send-temperature")
    if request.settings.enable_thinking is False:
        command.append("--openai-disable-thinking")
    if not request.dual_output:
        command.append("--no-dual")

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["SYNTHRECALL_OPENAI_API_KEY"] = request.api_key
    # B14:优先用调用方显式给定的 babeldoc_home;缺省才按 output_dir 层级推导
    babeldoc_home = (
        request.babeldoc_home
        if request.babeldoc_home is not None
        else output_dir.parents[1] / "babeldoc_home"
    )
    babeldoc_home.mkdir(parents=True, exist_ok=True)
    _copy_existing_babeldoc_assets(babeldoc_home)
    env["USERPROFILE"] = str(babeldoc_home)
    env["HOME"] = str(babeldoc_home)
    app_root = request.app_root if request.app_root is not None else _app_root()
    local_babeldoc = app_root / "BabelDOC-main"
    if local_babeldoc.exists():
        current = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(local_babeldoc) + (os.pathsep + current if current else "")

    process = subprocess.Popen(
        command,
        cwd=app_root,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=_subprocess_creationflags(),
    )
    output = ""
    while True:
        try:
            output, _ = process.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if request.cancel_requested is None or not request.cancel_requested():
                continue
            process.terminate()
            try:
                output, _ = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
            raise TranslationCancelledError("PDF 翻译已取消。")

    log_tail = _tail(output or "", 6000)
    if process.returncode != 0:
        raise RuntimeError(_friendly_failure_message(log_tail))

    result = find_translation_result(input_pdf, output_dir, request.lang_out)
    if result.mono_pdf is None and result.dual_pdf is None:
        raise RuntimeError(
            "BabelDOC 已结束，但没有找到生成的译文 PDF。"
            + (f"\n\n日志尾部：\n{log_tail}" if log_tail else "")
        )
    _validate_translation_language(input_pdf, result, request.lang_out, working_dir)
    # F20-1:翻译完全成功后清掉 _working 中间产物(实测可占译文相关占用
    # 的 ~77%,纯临时文件);失败路径不清理,保留日志供排查。
    shutil.rmtree(working_dir, ignore_errors=True)
    # F20-4:优先读 BabelDOC 落盘的真实计数(本地补丁 token_usage.json)。
    # 文件存在即视为真实值(含缓存命中时的 0——没调 API 就没花 token);
    # 文件缺失才退回日志解析,再退回字符估算。
    token_usage = _read_babeldoc_token_usage_file(output_dir)
    if token_usage is not None:
        return PDFTranslationResult(
            mono_pdf=result.mono_pdf,
            dual_pdf=result.dual_pdf,
            output_dir=output_dir,
            log_tail=log_tail,
            prompt_tokens=token_usage["prompt_tokens"],
            completion_tokens=token_usage["completion_tokens"],
            total_tokens=token_usage["total_tokens"],
        )
    token_usage = _parse_babeldoc_token_usage(output or "")
    if token_usage["total_tokens"] <= 0:
        estimated_total = max(1, len(_pdf_text_sample(input_pdf, max_pages=10_000)) // 3)
        return PDFTranslationResult(
            mono_pdf=result.mono_pdf,
            dual_pdf=result.dual_pdf,
            output_dir=output_dir,
            log_tail=log_tail,
            total_tokens=estimated_total,
            tokens_estimated=True,
        )
    return PDFTranslationResult(
        mono_pdf=result.mono_pdf,
        dual_pdf=result.dual_pdf,
        output_dir=output_dir,
        log_tail=log_tail,
        prompt_tokens=token_usage["prompt_tokens"],
        completion_tokens=token_usage["completion_tokens"],
        total_tokens=token_usage["total_tokens"],
    )


def _read_babeldoc_token_usage_file(output_dir: Path) -> dict[str, int] | None:
    """F20-4:读取 BabelDOC(本地补丁)落盘的真实 token 计数,读完即删。

    BabelDOC cli 的日志走有界异步队列(speed_up_logs),进程退出时最后的
    计数日志行可能被驱逐或未消费,靠日志解析不可靠;JSON 文件才是可靠通道。
    文件存在即返回(含全 0=翻译缓存命中,确实没调 API);缺失返回 None。
    """
    path = output_dir / "token_usage.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    def _int(key: str) -> int:
        try:
            return max(0, int(data.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    values = {
        "total_tokens": _int("total_tokens"),
        "prompt_tokens": _int("prompt_tokens"),
        "completion_tokens": _int("completion_tokens"),
    }
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return values


def _parse_babeldoc_token_usage(log_text: str) -> dict[str, int]:
    """F20-4:BabelDOC 翻译结束时 logger.info 输出 Total/Prompt/Completion
    tokens 三行(main.py:781-783);同一日志多文件时取最大值。"""
    values = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
    for match in re.finditer(r"(Total|Prompt|Completion) tokens:\s*(\d+)", log_text or ""):
        key = f"{match.group(1).lower()}_tokens"
        try:
            values[key] = max(values[key], int(match.group(2)))
        except ValueError:
            continue
    return values


def find_translation_result(
    input_pdf: Path,
    output_dir: Path,
    lang_out: str = "zh",
) -> PDFTranslationResult:
    output_dir = output_dir.resolve()
    stem = input_pdf.stem
    candidates = {
        "mono": [
            output_dir / f"{stem}.no_watermark.{lang_out}.mono.pdf",
            output_dir / f"{stem}.{lang_out}.mono.pdf",
            output_dir / "merged_no_watermark_mono.pdf",
            output_dir / "merged_mono.pdf",
        ],
        "dual": [
            output_dir / f"{stem}.no_watermark.{lang_out}.dual.pdf",
            output_dir / f"{stem}.{lang_out}.dual.pdf",
            output_dir / "merged_no_watermark_dual.pdf",
            output_dir / "merged_dual.pdf",
        ],
    }
    mono_pdf = next((path for path in candidates["mono"] if path.exists()), None)
    dual_pdf = next((path for path in candidates["dual"] if path.exists()), None)
    return PDFTranslationResult(mono_pdf=mono_pdf, dual_pdf=dual_pdf, output_dir=output_dir)


def _validate_translation_language(
    input_pdf: Path,
    result: PDFTranslationResult,
    lang_out: str,
    working_dir: Path,
) -> None:
    if lang_out.casefold() not in {"zh", "zh-cn", "zh-hans"}:
        return
    output_pdf = result.mono_pdf or result.dual_pdf
    if output_pdf is None:
        return
    source_text = _pdf_text_sample(input_pdf)
    output_text = _pdf_text_sample(output_pdf)
    source_latin = sum(character.isascii() and character.isalpha() for character in source_text)
    output_cjk = sum("\u4e00" <= character <= "\u9fff" for character in output_text)
    if source_latin < 30 or output_cjk >= 8:
        return
    detail = _translation_tracking_error(working_dir)
    suffix = f"\n\n翻译服务记录：{detail}" if detail else ""
    raise RuntimeError(
        "生成的 PDF 未检测到中文，已拒绝将英文回退结果标记为翻译完成。"
        "请检查模型权限或 API 配置后重试。"
        + suffix
    )


def _pdf_text_sample(path: Path, max_pages: int = 5) -> str:
    try:
        with fitz.open(path) as document:
            return "\n".join(
                document.load_page(index).get_text()
                for index in range(min(document.page_count, max_pages))
            )
    except Exception:  # noqa: BLE001 — 损坏 PDF 的抽样读取按空处理
        return ""


def _translation_tracking_error(working_dir: Path) -> str:
    for path in sorted(
        working_dir.rglob("translate_tracking.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        stack = [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                message = str(item.get("error_message") or "").strip()
                if item.get("has_error") and message:
                    return message[:600]
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return ""


def _python_executable() -> str:
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        console_python = executable.with_name("python.exe")
        if console_python.exists():
            return str(console_python)
    return str(executable)


def _babeldoc_command_prefix() -> list[str]:
    if getattr(sys, "frozen", False):
        app_dir = Path(sys.executable).resolve().parent
        for runner_name in ("ResearchWeaveBabelDOC.exe", "SynthRecallBabelDOC.exe"):
            runner = app_dir / runner_name
            if runner.exists():
                return [str(runner)]
        raise FileNotFoundError(
            "安装目录缺少 ResearchWeaveBabelDOC.exe，无法启动 PDF 翻译组件。"
        )
    return [_python_executable(), "-m", "babeldoc.main"]


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _subprocess_creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _copy_existing_babeldoc_assets(babeldoc_home: Path) -> None:
    source_cache = Path.home() / ".cache" / "babeldoc"
    target_cache = babeldoc_home / ".cache" / "babeldoc"
    if not source_cache.exists() or source_cache.resolve() == target_cache.resolve():
        return
    target_cache.mkdir(parents=True, exist_ok=True)
    for name in ("models", "fonts", "cmap", "tiktoken"):
        source = source_cache / name
        target = target_cache / name
        if not source.exists() or target.exists():
            continue
        try:
            shutil.copytree(source, target)
        except OSError:
            continue


def _tail(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _friendly_failure_message(log_tail: str) -> str:
    if not log_tail:
        return "BabelDOC 翻译失败，但没有返回错误日志。"
    hints = []
    if "No module named" in log_tail:
        hints.append("可能还有 BabelDOC 依赖没有安装完整。")
    if "Microsoft Visual C++ Redistributable" in log_tail or "DLL load failed" in log_tail:
        hints.append("可能需要安装 Microsoft Visual C++ Redistributable。")
    if "401" in log_tail or "Unauthorized" in log_tail:
        hints.append("请检查 API Key 是否正确。")
    if "404" in log_tail or "Not Found" in log_tail:
        hints.append("请检查 Base URL 和模型名称是否匹配。")
    hint_text = "\n".join(hints)
    return "BabelDOC 翻译失败。" + (f"\n{hint_text}" if hint_text else "") + f"\n\n日志尾部：\n{log_tail}"
