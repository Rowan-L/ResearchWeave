from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

KEYRING_SERVICE = "ResearchWeave"
LEGACY_KEYRING_SERVICE = "SynthRecall"
KEYRING_USERNAME = "llm_api_key"
KEYRING_PROVIDER_PREFIX = f"{KEYRING_USERNAME}:provider:"

DEFAULT_EVIDENCE_SECTIONS = ("摘要", "方法", "实验结果", "图表与表格")
DEFAULT_EVIDENCE_TYPES = ("研究目标", "方法", "数据集", "主要结果", "结论", "局限性")


@dataclass
class LLMProviderPreset:
    name: str
    base_url: str
    model: str
    api_key_placeholder: str = "sk-..."
    api_auth_style: str = "bearer"
    send_temperature: bool = True
    enable_thinking: bool | None = None
    model_catalog_supported: bool = True


DEFAULT_PROVIDER = LLMProviderPreset(
    "小红书 Dots",
    "https://note3-prev-api.askdiandian.com/v1",
    "dots3-note-prev",
    "从 Dots API 开放平台复制 Key",
    api_auth_style="api-key",
    send_temperature=False,
    enable_thinking=False,
    model_catalog_supported=False,
)

COMMON_LLM_PRESETS = [
    DEFAULT_PROVIDER,
    LLMProviderPreset("DeepSeek Chat", "https://api.deepseek.com", "deepseek-chat"),
    LLMProviderPreset("DeepSeek Reasoner", "https://api.deepseek.com", "deepseek-reasoner"),
    LLMProviderPreset("ChatGPT / OpenAI GPT-4o", "https://api.openai.com/v1", "gpt-4o"),
    LLMProviderPreset("ChatGPT / OpenAI GPT-4.1", "https://api.openai.com/v1", "gpt-4.1"),
    LLMProviderPreset("OpenAI o4-mini", "https://api.openai.com/v1", "o4-mini"),
    LLMProviderPreset("通义千问 Qwen Plus", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    LLMProviderPreset("通义千问 Qwen Max", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-max"),
    LLMProviderPreset("Moonshot Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    LLMProviderPreset("智谱 GLM-4", "https://open.bigmodel.cn/api/paas/v4", "glm-4-plus"),
]


@dataclass
class LLMSettings:
    # 新装/未配置时的默认 API 服务 = DEFAULT_PROVIDER(小红书 Dots);
    # 已有用户配置存于 llm_settings.json,加载时覆盖这些默认值
    base_url: str = DEFAULT_PROVIDER.base_url
    model: str = DEFAULT_PROVIDER.model
    api_auth_style: str = DEFAULT_PROVIDER.api_auth_style
    send_temperature: bool = DEFAULT_PROVIDER.send_temperature
    enable_thinking: bool | None = DEFAULT_PROVIDER.enable_thinking
    max_chars: int = 45_000
    prompt_template: str = ""
    translation_output_dir: str = ""
    # F20-3:默认只生成译文单本;双语对照体积翻倍,需要时再打开
    translation_dual_output: bool = False
    confirm_external_requests: bool = True
    require_source_evidence: bool = True
    distinguish_inference: bool = True
    leave_missing_blank: bool = True
    evidence_sections: list[str] = field(
        default_factory=lambda: list(DEFAULT_EVIDENCE_SECTIONS)
    )
    evidence_types: list[str] = field(
        default_factory=lambda: list(DEFAULT_EVIDENCE_TYPES)
    )

    @classmethod
    def load(cls, path: Path) -> LLMSettings:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        try:
            max_chars = int(data.get("max_chars") or cls.max_chars)
        except (TypeError, ValueError):
            max_chars = cls.max_chars
        max_chars = max(5_000, min(max_chars, 200_000))
        confirm_value = data.get("confirm_external_requests", True)
        if isinstance(confirm_value, str):
            confirm_external_requests = confirm_value.strip().casefold() not in {
                "0",
                "false",
                "no",
                "off",
            }
        else:
            confirm_external_requests = bool(confirm_value)

        def bool_value(key: str, default: bool) -> bool:
            value = data.get(key, default)
            if isinstance(value, str):
                return value.strip().casefold() not in {"0", "false", "no", "off"}
            return bool(value)

        def list_value(key: str, default: tuple[str, ...]) -> list[str]:
            if key not in data:
                return list(default)
            value = data.get(key)
            if not isinstance(value, list):
                return list(default)
            allowed = set(default)
            return [str(item) for item in value if str(item) in allowed]

        api_auth_style = str(data.get("api_auth_style") or "bearer").casefold()
        if api_auth_style not in {"bearer", "api-key"}:
            api_auth_style = "bearer"
        base_url = str(data.get("base_url") or cls.base_url)
        model = str(data.get("model") or cls.model)
        enable_thinking_value = data.get("enable_thinking")
        if isinstance(enable_thinking_value, bool):
            enable_thinking = enable_thinking_value
        elif (
            base_url.rstrip("/") == "https://note3-prev-api.askdiandian.com/v1"
            and model == "dots3-note-prev"
        ):
            # Migrate settings written by the first Dots preset implementation.
            enable_thinking = False
        else:
            enable_thinking = None
        return cls(
            base_url=base_url,
            model=model,
            api_auth_style=api_auth_style,
            send_temperature=bool_value("send_temperature", True),
            enable_thinking=enable_thinking,
            max_chars=max_chars,
            prompt_template=str(data.get("prompt_template") or ""),
            translation_output_dir=str(data.get("translation_output_dir") or ""),
            translation_dual_output=bool_value("translation_dual_output", False),
            confirm_external_requests=confirm_external_requests,
            require_source_evidence=bool_value("require_source_evidence", True),
            distinguish_inference=bool_value("distinguish_inference", True),
            leave_missing_blank=bool_value("leave_missing_blank", True),
            evidence_sections=list_value(
                "evidence_sections", DEFAULT_EVIDENCE_SECTIONS
            ),
            evidence_types=list_value("evidence_types", DEFAULT_EVIDENCE_TYPES),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(asdict(self), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            temporary_path.replace(path)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise


def normalize_llm_base_url(base_url: str) -> str:
    """Return a stable provider identity without exposing its URL in keyring names."""
    return base_url.strip().rstrip("/").casefold()


def _provider_keyring_username(base_url: str) -> str:
    normalized = normalize_llm_base_url(base_url)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{KEYRING_PROVIDER_PREFIX}{digest}"


def get_llm_api_key(base_url: str = "", *, allow_legacy: bool = True) -> str:
    try:
        import keyring

        if normalize_llm_base_url(base_url):
            api_key = keyring.get_password(
                KEYRING_SERVICE, _provider_keyring_username(base_url)
            ) or ""
            if api_key or not allow_legacy:
                return api_key
        api_key = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME) or ""
        if api_key:
            return api_key
        return keyring.get_password(LEGACY_KEYRING_SERVICE, KEYRING_USERNAME) or ""
    except Exception:  # noqa: BLE001 — keyring 后端差异大,读取失败按无 Key 处理
        return ""


def set_llm_api_key(api_key: str, base_url: str = "") -> None:
    try:
        import keyring

        username = (
            _provider_keyring_username(base_url)
            if normalize_llm_base_url(base_url)
            else KEYRING_USERNAME
        )
        if api_key:
            keyring.set_password(KEYRING_SERVICE, username, api_key)
        else:
            keyring.delete_password(KEYRING_SERVICE, username)
    except Exception as exc:
        if not api_key:
            try:
                import keyring.errors

                if isinstance(exc, keyring.errors.PasswordDeleteError):
                    return
            except Exception:  # noqa: BLE001,S110 — 同上,清理失败不阻塞
                pass
        raise RuntimeError(f"无法安全保存 API Key：{exc}") from exc


def llm_api_key_header(api_auth_style: str, api_key: str) -> tuple[str, str]:
    """Return the configured provider's API-key header name and value."""
    if api_auth_style == "api-key":
        return "api-key", api_key
    return "Authorization", f"Bearer {api_key}"
