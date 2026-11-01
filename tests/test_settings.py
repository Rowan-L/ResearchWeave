from __future__ import annotations

import keyring

from synthrecall.settings import (
    COMMON_LLM_PRESETS,
    DEFAULT_PROVIDER,
    LLMSettings,
    get_llm_api_key,
    set_llm_api_key,
)


def test_llm_settings_default_provider_is_dots():
    """用户指定:新装/未配置时 API 默认 = 小红书 Dots(预置列表首位)。"""
    settings = LLMSettings()
    assert settings.base_url == DEFAULT_PROVIDER.base_url
    assert settings.model == "dots3-note-prev"
    assert settings.api_auth_style == "api-key"
    assert settings.send_temperature is False
    assert settings.enable_thinking is False
    assert COMMON_LLM_PRESETS[0] is DEFAULT_PROVIDER
    assert DEFAULT_PROVIDER.name == "小红书 Dots"


def test_api_keys_are_scoped_by_normalized_base_url(monkeypatch):
    credentials: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda service, username: credentials.get((service, username)),
    )
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda service, username, value: credentials.__setitem__(
            (service, username), value
        ),
    )

    set_llm_api_key("deepseek-key", "https://api.deepseek.com/")
    set_llm_api_key("dots-key", "https://note3-prev-api.askdiandian.com/v1")

    assert get_llm_api_key("https://api.deepseek.com", allow_legacy=False) == (
        "deepseek-key"
    )
    assert get_llm_api_key(
        "https://note3-prev-api.askdiandian.com/v1/", allow_legacy=False
    ) == "dots-key"
    assert get_llm_api_key("https://api.openai.com/v1", allow_legacy=False) == ""


def test_provider_key_can_fall_back_to_legacy_global_key(monkeypatch):
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda service, username: "legacy-key" if username == "llm_api_key" else None,
    )

    assert get_llm_api_key("https://api.deepseek.com") == "legacy-key"
    assert (
        get_llm_api_key("https://api.deepseek.com", allow_legacy=False) == ""
    )
