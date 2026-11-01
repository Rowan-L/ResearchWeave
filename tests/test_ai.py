from types import SimpleNamespace
from typing import ClassVar

import pytest

from synthrecall.ai import (
    LLMClient,
    PaperAIAnalysis,
    model_catalog_endpoint,
    parse_json_object,
    parse_model_catalog,
    strip_markdown_fence,
)
from synthrecall.database import create_session_factory, init_database
from synthrecall.repositories import LibraryRepository
from synthrecall.seed import seed_demo_data
from synthrecall.settings import LLMSettings


def test_parse_json_object_from_markdown_fence():
    data = parse_json_object(
        """
```json
{"title": "Demo", "datasets": ["ImageNet", "COCO"]}
```
"""
    )

    assert data["title"] == "Demo"
    assert data["datasets"] == ["ImageNet", "COCO"]


def test_parse_json_object_repairs_invalid_latex_and_path_escapes():
    data = parse_json_object(
        r'''{"methods":["uses \alpha and C:\models\paper"],"summary":["ok"]}'''
    )

    assert data["methods"] == [r"uses \alpha and C:\models\paper"]


def test_strip_markdown_fence_for_plain_translation():
    assert strip_markdown_fence("```text\n中文译文\n```") == "中文译文"
    assert strip_markdown_fence("中文译文") == "中文译文"


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.example.com/v1", "https://api.example.com/v1/models"),
        ("https://api.example.com/v1/", "https://api.example.com/v1/models"),
        (
            "https://api.example.com/v1/chat/completions",
            "https://api.example.com/v1/models",
        ),
        ("https://api.example.com/v1/responses", "https://api.example.com/v1/models"),
    ],
)
def test_model_catalog_endpoint_accepts_common_base_urls(base_url, expected):
    assert model_catalog_endpoint(base_url) == expected


def test_parse_model_catalog_supports_openai_and_alternative_shapes():
    assert parse_model_catalog(
        {"data": [{"id": "deepseek-pro"}, {"id": "deepseek-flash"}]}
    ) == ["deepseek-flash", "deepseek-pro"]
    assert parse_model_catalog(
        {"models": [{"name": "model-b"}, "model-a", {"id": "model-a"}]}
    ) == ["model-a", "model-b"]


def test_parse_model_catalog_rejects_unrecognized_payload():
    with pytest.raises(TypeError, match="data 或 models 数组"):
        parse_model_catalog({"result": "ok"})


def test_apply_ai_analysis_updates_paper_detail(tmp_path):
    session_factory = create_session_factory(tmp_path / "library.db")
    init_database(session_factory)
    seed_demo_data(session_factory)
    repository = LibraryRepository(session_factory)
    paper = repository.create_blank_paper()

    repository.apply_ai_analysis(
        paper.id,
        PaperAIAnalysis(
            {
                "title": "AI Parsed Paper",
                "authors": ["Ada", "Turing"],
                "year": 2026,
                "venue": "SynthRecall Test",
                "abstract": "Parsed abstract",
                "summary": "Parsed summary",
                "datasets": ["ImageNet"],
                "dataset_metrics": [
                    {
                        "dataset": "ImageNet",
                        "metric": "Top-1 Acc",
                        "value": "84.5%",
                        "model": "DemoNet",
                    }
                ],
                "methods": ["Transformer"],
                "contributions": ["Contribution"],
                "limitations": ["Limitation"],
                "experiment_notes": "Experiment notes",
                "reproducibility_notes": "Reproducibility notes",
                "keywords": ["keyword-a", "keyword-b"],
                "memory_anchor": ["future clue", "important trick"],
            }
        ),
    )

    updated = repository.get_paper(paper.id)

    assert updated.title == "AI Parsed Paper"
    assert updated.authors == "Ada; Turing"
    assert updated.detail.summary == "Parsed summary"
    assert updated.detail.datasets == "ImageNet"
    assert updated.detail.dataset_metrics == ""
    assert updated.detail.memory_anchor == "future clue; important trick; 数据集：ImageNet"
    assert [category.name for category in updated.categories] == ["未分类"]
    assert "AI关键词" in updated.detail.personal_notes


def test_ai_analysis_formats_detailed_lists_as_bullets():
    analysis = PaperAIAnalysis(
        {
            "summary": ["研究问题：demo", {"方法": "dynamic kernel", "作用": "生成卷积核"}],
            "datasets": [{"name": "ImageNet", "task": "classification"}],
        }
    )

    assert analysis.summary == "- 研究问题：demo\n- 方法: dynamic kernel; 作用: 生成卷积核"
    assert analysis.datasets == "name: ImageNet; task: classification"


def test_prompt_requests_detailed_accurate_notes():
    client = LLMClient(LLMSettings(max_chars=1000), api_key="test")
    prompt = client._build_user_content(
        {
            "title": "Demo Paper",
            "abstract": "Demo abstract",
            "text": "paper text",
        }
    )

    assert "研究问题/任务背景" in prompt
    assert "不要编造" in prompt
    assert "memory_anchor" in prompt
    assert "不要输出 category、categories" in prompt
    assert "半年后" in prompt
    assert "不要输出 dataset_metrics" in prompt
    assert "关键结论必须给出原文可定位依据" in prompt
    assert "所有推断以“推断：”开头" in prompt
    assert "论文未提供的信息必须留空" in prompt


def test_prompt_runtime_rules_follow_workflow_switches():
    client = LLMClient(
        LLMSettings(
            max_chars=1000,
            require_source_evidence=False,
            distinguish_inference=True,
            leave_missing_blank=False,
        ),
        api_key="test",
    )

    prompt = client._build_user_content({"title": "Demo", "text": "paper text"})

    assert "关键结论必须给出原文可定位依据" not in prompt
    assert "所有推断以“推断：”开头" in prompt
    assert "论文未提供的信息必须留空" not in prompt


def test_discuss_paper_builds_grounded_multiturn_request(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {"message": {"content": "基于论文的回答"}}
                ]
            }

    class FakeClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, endpoint, headers, json):
            captured["endpoint"] = endpoint
            captured["headers"] = headers
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr("synthrecall.ai.httpx.Client", FakeClient)
    client = LLMClient(
        LLMSettings(
            base_url="https://example.test/v1",
            model="test-model",
            max_chars=100,
        ),
        api_key="test-key",
    )

    answer = client.discuss_paper(
        {
            "title": "Demo",
            "abstract": "Abstract",
            "personal_notes": "My note",
            "text": "paper body",
        },
        [{"role": "user", "content": "Earlier question"}],
        "What is the key contribution?",
    )

    assert answer == "基于论文的回答"
    assert captured["endpoint"] == "https://example.test/v1/chat/completions"
    assert captured["payload"]["model"] == "test-model"
    messages = captured["payload"]["messages"]
    assert "只基于提供的论文内容" in messages[0]["content"]
    assert "paper body" in messages[1]["content"]
    assert messages[-2] == {"role": "user", "content": "Earlier question"}
    assert messages[-1]["content"] == "What is the key contribution?"


def test_llm_client_reports_malformed_provider_response(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": []}

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr("synthrecall.ai.httpx.Client", FakeClient)
    client = LLMClient(LLMSettings(), api_key="test-key")

    with pytest.raises(RuntimeError, match="choices/message/content"):
        client.translate_abstract_to_zh("abstract")


def test_dots_client_uses_api_key_header_and_omits_temperature(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "中文译文"}}]}

    class FakeClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, endpoint, headers, json):
            captured.update(endpoint=endpoint, headers=headers, payload=json)
            return FakeResponse()

    monkeypatch.setattr("synthrecall.ai.httpx.Client", FakeClient)
    client = LLMClient(
        LLMSettings(
            base_url="https://note3-prev-api.askdiandian.com/v1",
            model="dots3-note-prev",
            api_auth_style="api-key",
            send_temperature=False,
            enable_thinking=False,
        ),
        api_key="dots-secret",
    )

    assert client.translate_abstract_to_zh("Abstract") == "中文译文"
    assert captured["endpoint"].endswith("/v1/chat/completions")
    assert captured["headers"]["api-key"] == "dots-secret"
    assert "Authorization" not in captured["headers"]
    assert "temperature" not in captured["payload"]
    assert captured["payload"]["chat_template_kwargs"] == {
        "enable_thinking": False
    }


def test_chat_completion_retries_on_429_and_5xx(monkeypatch):
    from synthrecall.ai import LLMClient
    from synthrecall.settings import LLMSettings

    client = LLMClient(LLMSettings(base_url="https://example.invalid/v1", model="m"), "key")
    calls = []
    sleeps: list[float] = []

    class FakeResponse:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}

        def raise_for_status(self):
            if self.status_code < 400:
                return
            import httpx

            request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
            raise httpx.HTTPStatusError("err", request=request, response=httpx.Response(self.status_code, request=request))

    def fake_post(self, url, headers=None, json=None):
        calls.append(url)
        if len(calls) < 3:
            return FakeResponse(503)
        ok = FakeResponse(200)
        ok.json = lambda: {"choices": [{"message": {"content": "回复"}}]}
        return ok

    monkeypatch.setattr("synthrecall.ai.httpx.Client", lambda timeout: _FakeHttpClient(fake_post, timeout))
    monkeypatch.setattr("synthrecall.ai.time.sleep", lambda s: sleeps.append(s))

    result = client._chat_completion({"model": "m"}, timeout=5)
    assert result == "回复"
    assert len(calls) == 3, "前两次 503 应重试"
    assert len(sleeps) == 2, "两次重试各等待一次"


def test_chat_completion_does_not_retry_on_401(monkeypatch):
    import httpx
    import pytest

    from synthrecall.ai import LLMClient
    from synthrecall.settings import LLMSettings

    client = LLMClient(LLMSettings(base_url="https://example.invalid/v1", model="m"), "key")
    calls = []

    class FakeResponse:
        status_code: ClassVar[int] = 401
        headers: ClassVar[dict] = {}

        def raise_for_status(self):
            request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
            raise httpx.HTTPStatusError("err", request=request, response=httpx.Response(401, request=request))

    def fake_post(self, url, headers=None, json=None):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr("synthrecall.ai.httpx.Client", lambda timeout: _FakeHttpClient(fake_post, timeout))
    monkeypatch.setattr("synthrecall.ai.time.sleep", lambda s: (_ for _ in ()).throw(AssertionError("401 不应重试")))

    with pytest.raises(RuntimeError, match="401"):
        client._chat_completion({"model": "m"}, timeout=5)
    assert len(calls) == 1, "配置错误不应重试"


class _FakeHttpClient:
    def __init__(self, post, timeout):
        self._post = post
        self._timeout = timeout

    def post(self, url, headers=None, json=None):
        return self._post(self, url, headers=headers, json=json)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# ---------------- B23 流式输出 ----------------


def test_iter_sse_content_deltas_parses_and_tolerates_noise():
    from synthrecall.ai import iter_sse_content_deltas

    lines = [
        ": heartbeat",
        "",
        'data: {"choices": [{"delta": {"content": "你好"}}]}',
        'data: {"choices": [{"delta": {}}]}',
        "data: not-json",
        'data: {"choices": [{"delta": {"content": ",世界"}}]}',
        "data: [DONE]",
        'data: {"choices": [{"delta": {"content": "不应出现"}}]}',
    ]
    assert list(iter_sse_content_deltas(lines)) == ["你好", ",世界"]


class _FakeStreamResponse:
    def __init__(self, lines, status_error=None):
        self._lines = lines
        self._status_error = status_error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error

    def iter_lines(self):
        return iter(self._lines)


class _FakeStreamClient:
    """先返回一次流式响应,再返回一次非流式回退响应。"""

    def __init__(self, stream_lines, plain_text="回退全文", stream_fail=False, *unused):
        self.stream_lines = stream_lines
        self.plain_text = plain_text
        self.stream_fail = stream_fail
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method, endpoint, headers=None, json=None):
        self.calls.append({"kind": "stream", "payload": json})
        if self.stream_fail:
            import httpx

            request = httpx.Request("POST", endpoint)
            response = httpx.Response(502, request=request)
            raise httpx.HTTPStatusError("server error", request=request, response=response)
        return _FakeStreamResponse(self.stream_lines)

    def post(self, endpoint, headers, json):
        self.calls.append({"kind": "plain", "payload": json})
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": self.plain_text}}]},
        )


def _make_client(monkeypatch, fake):
    monkeypatch.setattr("synthrecall.ai.httpx.Client", lambda *args, **kwargs: fake)
    return LLMClient(
        LLMSettings(base_url="https://example.test/v1", model="m", max_chars=100),
        api_key="key",
    )


def test_streaming_completion_accumulates_deltas(monkeypatch):
    lines = [
        'data: {"choices": [{"delta": {"content": "第一"}}]}',
        'data: {"choices": [{"delta": {"content": "部分"}}]}',
        "data: [DONE]",
    ]
    fake = _FakeStreamClient(lines)
    client = _make_client(monkeypatch, fake)

    received: list[str] = []
    answer = client.discuss_paper(
        {"title": "t", "abstract": "a", "text": "x"}, [], "问", on_delta=received.append
    )
    assert answer == "第一部分"
    assert received == ["第一", "部分"]
    assert fake.calls[0]["payload"]["stream"] is True
    assert len(fake.calls) == 1, "流式成功不应再发非流式请求"


def test_streaming_falls_back_to_plain_on_gateway_error(monkeypatch):
    fake = _FakeStreamClient([], plain_text="非流式回答", stream_fail=True)
    client = _make_client(monkeypatch, fake)

    received: list[str] = []
    answer = client.discuss_paper(
        {"title": "t", "abstract": "a", "text": "x"}, [], "问", on_delta=received.append
    )
    assert answer == "非流式回答"
    assert received == [], "回退路径不应产生流式增量"
    assert [call["kind"] for call in fake.calls] == ["stream", "plain"]
    assert "stream" not in fake.calls[1]["payload"], "回退请求应为非流式"
