from pathlib import Path

import pytest

from synthrecall.settings import COMMON_LLM_PRESETS, LLMSettings
from synthrecall.translation import (
    PDFTranslationRequest,
    TranslationCancelledError,
    _babeldoc_command_prefix,
    _validate_translation_language,
    find_translation_result,
    translate_pdf_with_babeldoc,
)


def _write_text_pdf(path: Path, text: str) -> None:
    import pymupdf as fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontname="china-s")
    document.save(path)
    document.close()


def test_find_translation_result_prefers_no_watermark_outputs(tmp_path):
    input_pdf = Path("demo.pdf")
    mono = tmp_path / "demo.no_watermark.zh.mono.pdf"
    dual = tmp_path / "demo.no_watermark.zh.dual.pdf"
    mono.write_bytes(b"%PDF mono")
    dual.write_bytes(b"%PDF dual")

    result = find_translation_result(input_pdf, tmp_path)

    assert result.mono_pdf == mono
    assert result.dual_pdf == dual


def test_find_translation_result_supports_merged_outputs(tmp_path):
    dual = tmp_path / "merged_no_watermark_dual.pdf"
    dual.write_bytes(b"%PDF dual")

    result = find_translation_result(Path("split.pdf"), tmp_path)

    assert result.mono_pdf is None
    assert result.dual_pdf == dual


def test_translation_validation_rejects_english_fallback_output(tmp_path):
    source = tmp_path / "source.pdf"
    mono = tmp_path / "source.no_watermark.zh.mono.pdf"
    english = "This is an English research paragraph. " * 8
    _write_text_pdf(source, english)
    _write_text_pdf(mono, english)
    result = find_translation_result(source, tmp_path)

    with pytest.raises(RuntimeError, match="未检测到中文"):
        _validate_translation_language(source, result, "zh", tmp_path)


def test_translation_validation_accepts_chinese_output(tmp_path):
    source = tmp_path / "source.pdf"
    mono = tmp_path / "source.no_watermark.zh.mono.pdf"
    _write_text_pdf(source, "This is an English research paragraph. " * 8)
    _write_text_pdf(mono, "这是已经正确翻译为中文的研究论文段落。" * 8)
    result = find_translation_result(source, tmp_path)

    _validate_translation_language(source, result, "zh", tmp_path)


def test_llm_settings_persists_translation_output_dir(tmp_path):
    settings_path = tmp_path / "llm_settings.json"
    output_dir = tmp_path / "translated"

    LLMSettings(translation_output_dir=str(output_dir)).save(settings_path)
    loaded = LLMSettings.load(settings_path)

    assert loaded.translation_output_dir == str(output_dir)


def test_llm_settings_persists_provider_protocol_options(tmp_path):
    settings_path = tmp_path / "llm_settings.json"
    LLMSettings(
        api_auth_style="api-key", send_temperature=False, enable_thinking=False
    ).save(settings_path)

    loaded = LLMSettings.load(settings_path)

    assert loaded.api_auth_style == "api-key"
    assert loaded.send_temperature is False
    assert loaded.enable_thinking is False


def test_existing_dots_settings_migrate_to_non_thinking_requests(tmp_path):
    settings_path = tmp_path / "llm_settings.json"
    settings_path.write_text(
        '{"base_url":"https://note3-prev-api.askdiandian.com/v1",'
        '"model":"dots3-note-prev","api_auth_style":"api-key",'
        '"send_temperature":false}',
        encoding="utf-8",
    )

    loaded = LLMSettings.load(settings_path)

    assert loaded.enable_thinking is False


def test_llm_settings_recovers_from_invalid_scalar_values(tmp_path):
    settings_path = tmp_path / "llm_settings.json"
    settings_path.write_text(
        '{"max_chars": "broken", "confirm_external_requests": "false"}',
        encoding="utf-8",
    )

    loaded = LLMSettings.load(settings_path)

    assert loaded.max_chars == LLMSettings.max_chars
    assert loaded.confirm_external_requests is False


def test_common_llm_presets_include_chatgpt_deepseek_and_dots():
    presets = {(preset.base_url, preset.model) for preset in COMMON_LLM_PRESETS}

    assert ("https://api.deepseek.com", "deepseek-chat") in presets
    assert ("https://api.openai.com/v1", "gpt-4o") in presets
    dots = next(preset for preset in COMMON_LLM_PRESETS if preset.name == "小红书 Dots")
    assert dots.base_url == "https://note3-prev-api.askdiandian.com/v1"
    assert dots.model == "dots3-note-prev"
    assert dots.api_auth_style == "api-key"
    assert dots.send_temperature is False
    assert dots.enable_thinking is False
    assert dots.model_catalog_supported is False


def test_babeldoc_translation_can_terminate_subprocess(tmp_path, monkeypatch):
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF test")
    terminated: list[bool] = []

    class FakeProcess:
        returncode = None

        def __init__(self, *_args, **_kwargs):
            self.communicate_calls = 0

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise __import__("subprocess").TimeoutExpired("babeldoc", timeout)
            self.returncode = -1
            return "partial output", None

        def terminate(self):
            terminated.append(True)

        def kill(self):
            raise AssertionError("terminate should be sufficient")

    monkeypatch.setattr("synthrecall.translation.subprocess.Popen", FakeProcess)
    monkeypatch.setattr(
        "synthrecall.translation._babeldoc_command_prefix",
        lambda: ["babeldoc"],
    )
    monkeypatch.setattr("synthrecall.translation._app_root", lambda: tmp_path)
    monkeypatch.setattr("synthrecall.translation._copy_existing_babeldoc_assets", lambda *_: None)

    request = PDFTranslationRequest(
        input_pdf=input_pdf,
        output_dir=tmp_path / "output",
        settings=LLMSettings(),
        api_key="test-key",
        cancel_requested=lambda: True,
    )
    with pytest.raises(TranslationCancelledError, match="已取消"):
        translate_pdf_with_babeldoc(request)

    assert terminated == [True]


def test_babeldoc_translation_keeps_api_key_out_of_command_line(tmp_path, monkeypatch):
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(b"%PDF test")
    captured: dict[str, object] = {}

    class FakeProcess:
        returncode = 1

        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]

        def communicate(self, timeout=None):
            return "expected failure", None

    monkeypatch.setattr("synthrecall.translation.subprocess.Popen", FakeProcess)
    monkeypatch.setattr(
        "synthrecall.translation._babeldoc_command_prefix",
        lambda: ["babeldoc"],
    )
    monkeypatch.setattr("synthrecall.translation._app_root", lambda: tmp_path)
    monkeypatch.setattr("synthrecall.translation._copy_existing_babeldoc_assets", lambda *_: None)

    request = PDFTranslationRequest(
        input_pdf=input_pdf,
        output_dir=tmp_path / "output",
        settings=LLMSettings(
            api_auth_style="api-key", send_temperature=False, enable_thinking=False
        ),
        api_key="secret-test-key",
    )
    with pytest.raises(RuntimeError, match="BabelDOC 翻译失败"):
        translate_pdf_with_babeldoc(request)

    command = captured["command"]
    env = captured["env"]
    assert "secret-test-key" not in command
    assert "--openai-api-key" not in command
    assert command[command.index("--openai-api-key-header") + 1] == "api-key"
    assert "--no-send-temperature" in command
    assert "--openai-disable-thinking" in command
    assert env["SYNTHRECALL_OPENAI_API_KEY"] == "secret-test-key"


def test_frozen_babeldoc_command_uses_dedicated_runner(tmp_path, monkeypatch):
    app = tmp_path / "ResearchWeave.exe"
    runner = tmp_path / "ResearchWeaveBabelDOC.exe"
    app.write_bytes(b"")
    runner.write_bytes(b"")
    monkeypatch.setattr("synthrecall.translation.sys.executable", str(app))
    monkeypatch.setattr("synthrecall.translation.sys.frozen", True, raising=False)

    assert _babeldoc_command_prefix() == [str(runner)]


def test_frozen_babeldoc_command_reports_missing_component(tmp_path, monkeypatch):
    app = tmp_path / "ResearchWeave.exe"
    app.write_bytes(b"")
    monkeypatch.setattr("synthrecall.translation.sys.executable", str(app))
    monkeypatch.setattr("synthrecall.translation.sys.frozen", True, raising=False)

    with pytest.raises(FileNotFoundError, match="ResearchWeaveBabelDOC.exe"):
        _babeldoc_command_prefix()


def test_frozen_babeldoc_command_accepts_legacy_runner(tmp_path, monkeypatch):
    app = tmp_path / "ResearchWeave.exe"
    legacy_runner = tmp_path / "SynthRecallBabelDOC.exe"
    app.write_bytes(b"")
    legacy_runner.write_bytes(b"")
    monkeypatch.setattr("synthrecall.translation.sys.executable", str(app))
    monkeypatch.setattr("synthrecall.translation.sys.frozen", True, raising=False)

    assert _babeldoc_command_prefix() == [str(legacy_runner)]


def test_request_honors_explicit_babeldoc_home_and_app_root(tmp_path, monkeypatch):
    """B14 回归:显式给定的 babeldoc_home/app_root 优先,不再依赖 output_dir 层级。"""

    output_dir = tmp_path / "deep" / "nested" / "output"
    explicit_home = tmp_path / "custom-home"
    explicit_root = tmp_path / "custom-root"
    input_pdf = tmp_path / "in.pdf"
    import pymupdf as fitz

    doc = fitz.open()
    doc.new_page()
    doc.save(input_pdf)
    doc.close()

    captured_env = {}

    def fake_popen(command, cwd, env, **kwargs):
        captured_env["cwd"] = cwd
        captured_env["home"] = env["HOME"]
        class P:
            def communicate(self, timeout=None):
                return ("boom", None)
            returncode = 1
        return P()

    monkeypatch.setattr("synthrecall.translation.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "synthrecall.translation._copy_existing_babeldoc_assets", lambda *_: None
    )

    request = PDFTranslationRequest(
        input_pdf=input_pdf,
        output_dir=output_dir,
        settings=LLMSettings(),
        api_key="k",
        babeldoc_home=explicit_home,
        app_root=explicit_root,
    )
    with pytest.raises(RuntimeError):
        translate_pdf_with_babeldoc(request)

    assert captured_env["home"] == str(explicit_home), "HOME 应使用显式 babeldoc_home"
    assert Path(captured_env["cwd"]) == explicit_root, "cwd 应使用显式 app_root"
    assert explicit_home.exists(), "babeldoc_home 应被创建"
    # 旧推导路径不得被创建(层级假设已解除)
    assert not output_dir.parents[1].joinpath("babeldoc_home").exists()
