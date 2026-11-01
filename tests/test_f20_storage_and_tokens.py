"""F20 回归:存储管理 + token 用量统计 + 翻译产物策略。

- 翻译成功清 _working、失败保留;--no-dual 按设置开关;
- BabelDOC 日志 token 解析与字符估算;
- LLMClient 用量回调(非流式真实 usage / 缺失时估算);
- 仓库层四桶汇总(今日/近7天/近30天/累计);
- 存储扫描分类与安全清理(只动派生物,不碰 db/papers)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pymupdf as fitz
import pytest

import synthrecall.ai as ai_module
import synthrecall.translation as translation_module
from synthrecall.ai import LLMClient, _estimate_tokens_for, iter_sse_payloads
from synthrecall.database import create_session_factory, init_database
from synthrecall.repositories import LibraryRepository
from synthrecall.settings import LLMSettings
from synthrecall.translation import (
    PDFTranslationRequest,
    _parse_babeldoc_token_usage,
    translate_pdf_with_babeldoc,
)


def _write_english_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Attention Is All You Need: an English abstract sample text.")
    document.save(path)
    document.close()


def _write_chinese_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "这是一段用于校验的中文译文内容。", fontname="china-s")
    document.save(path)
    document.close()


# ---------------------------------------------------------------------------
# F20-1/F20-3 翻译产物
# ---------------------------------------------------------------------------


class _FakePopen:
    def __init__(self, command, *, returncode: int, log: str, **_kwargs):
        self.command = command
        self.returncode = returncode
        self._log = log

    def communicate(self, timeout=None):
        return (self._log, None)


def _run_fake_translation(
    tmp_path: Path,
    monkeypatch,
    *,
    returncode: int,
    log: str,
    dual_output: bool = False,
):
    input_pdf = tmp_path / "paper.pdf"
    _write_english_pdf(input_pdf)
    output_dir = tmp_path / "out"
    working = output_dir / "_working"
    working.mkdir(parents=True)
    (working / "temp_subset_input_mono.pdf").write_bytes(b"junk")
    # 预置成功产物:mono 必须存在且含中文,验证函数才会判定成功
    _write_chinese_pdf(output_dir / "paper.no_watermark.zh.mono.pdf")

    captured: dict = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return _FakePopen(command, returncode=returncode, log=log)

    monkeypatch.setattr(translation_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(translation_module, "_copy_existing_babeldoc_assets", lambda _h: None)

    request = PDFTranslationRequest(
        input_pdf=input_pdf,
        output_dir=output_dir,
        settings=LLMSettings(base_url="http://unit.test", model="unit-model"),
        api_key="unit-key",
        dual_output=dual_output,
        babeldoc_home=tmp_path / "bh",
        app_root=tmp_path,
    )
    return captured, request


def test_translation_success_cleans_working_and_parses_tokens(tmp_path, monkeypatch):
    log = "everything fine\nTotal tokens: 1234\nPrompt tokens: 1000\nCompletion tokens: 234\n"
    captured, request = _run_fake_translation(tmp_path, monkeypatch, returncode=0, log=log)

    result = translate_pdf_with_babeldoc(request)

    assert result.total_tokens == 1234
    assert result.prompt_tokens == 1000
    assert result.completion_tokens == 234
    assert result.tokens_estimated is False
    # F20-1:成功后 _working 被清掉
    assert not (tmp_path / "out" / "_working").exists()
    # F20-3:默认只出译文单本
    assert "--no-dual" in captured["command"]


def test_translation_dual_output_flag_controls_command(tmp_path, monkeypatch):
    log = "Total tokens: 5\n"
    captured, request = _run_fake_translation(
        tmp_path, monkeypatch, returncode=0, log=log, dual_output=True
    )
    translate_pdf_with_babeldoc(request)
    assert "--no-dual" not in captured["command"]


def test_translation_failure_keeps_working_dir(tmp_path, monkeypatch):
    log = "some error\nTotal tokens: 7\n"
    _captured, request = _run_fake_translation(tmp_path, monkeypatch, returncode=1, log=log)

    with pytest.raises(RuntimeError, match="BabelDOC 翻译失败"):
        translate_pdf_with_babeldoc(request)
    # F20-1:失败保留 _working 供排查
    assert (tmp_path / "out" / "_working").exists()


def test_translation_prefers_token_usage_json_over_log(tmp_path, monkeypatch):
    """F20-4:BabelDOC 本地补丁落盘的 token_usage.json 是可靠通道,优先于
    日志解析,读完即删。"""
    import json as json_module

    log = "Total tokens: 999\n"  # 日志里的值应被 JSON 覆盖
    _captured, request = _run_fake_translation(tmp_path, monkeypatch, returncode=0, log=log)
    (tmp_path / "out" / "token_usage.json").write_text(
        json_module.dumps(
            {"total_tokens": 500, "prompt_tokens": 450, "completion_tokens": 50}
        ),
        encoding="utf-8",
    )

    result = translate_pdf_with_babeldoc(request)

    assert result.total_tokens == 500
    assert result.prompt_tokens == 450
    assert result.completion_tokens == 50
    assert result.tokens_estimated is False
    assert not (tmp_path / "out" / "token_usage.json").exists()


def test_translation_cache_hit_reports_zero_tokens(tmp_path, monkeypatch):
    """BabelDOC 翻译缓存命中 = 没调 API:JSON 全 0 是真实值,不是估算。"""
    import json as json_module

    log = "Total tokens: 999\n"
    _captured, request = _run_fake_translation(tmp_path, monkeypatch, returncode=0, log=log)
    (tmp_path / "out" / "token_usage.json").write_text(
        json_module.dumps({"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}),
        encoding="utf-8",
    )

    result = translate_pdf_with_babeldoc(request)

    assert result.total_tokens == 0
    assert result.prompt_tokens == 0
    assert result.tokens_estimated is False
    assert not (tmp_path / "out" / "token_usage.json").exists()


def test_parse_babeldoc_token_usage_takes_max_and_tolerates_missing():
    log = "part1 Total tokens: 100\npart2 Total tokens: 900\nPrompt tokens: 800\nCompletion tokens: 90\n"
    values = _parse_babeldoc_token_usage(log)
    assert values == {
        "total_tokens": 900,
        "prompt_tokens": 800,
        "completion_tokens": 90,
    }
    assert _parse_babeldoc_token_usage("no tokens mentioned") == {
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


# ---------------------------------------------------------------------------
# F20-4 LLMClient 用量
# ---------------------------------------------------------------------------


class _FakePlainClient:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        return self

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _plain_client_monkeypatch(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(ai_module.httpx, "Client", lambda *a, **k: _FakePlainClient(payload))


def test_llm_client_reports_real_usage_from_plain_response(monkeypatch):
    _plain_client_monkeypatch(
        monkeypatch,
        {
            "choices": [{"message": {"content": "回答内容"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        },
    )
    recorded: list[dict] = []
    client = LLMClient(
        LLMSettings(base_url="http://unit.test", model="unit-model"),
        "key",
        usage_callback=recorded.append,
    )
    content = client._request_plain(
        {"messages": [{"role": "user", "content": "问题"}]}, timeout=1
    )
    assert content == "回答内容"
    assert recorded == [
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "estimated": False,
            "model": "unit-model",
        }
    ]


def test_llm_client_estimates_when_usage_missing(monkeypatch):
    _plain_client_monkeypatch(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})
    recorded: list[dict] = []
    client = LLMClient(
        LLMSettings(base_url="http://unit.test", model="unit-model"),
        "key",
        usage_callback=recorded.append,
    )
    client._request_plain(
        {"messages": [{"role": "user", "content": "x" * 300}]}, timeout=1
    )
    assert len(recorded) == 1
    usage = recorded[0]
    assert usage["estimated"] is True
    assert usage["prompt_tokens"] == 100  # 300 // 3
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_estimate_tokens_and_sse_payloads():
    assert _estimate_tokens_for("") == 1
    assert _estimate_tokens_for("x" * 300) == 100
    lines = [
        'data: {"choices":[{"delta":{"content":"hi"}}]}',
        "data: [DONE]",
        'data: {"choices":[],"usage":{"total_tokens":9}}',
    ]
    payloads = list(iter_sse_payloads(lines))
    assert payloads[0]["choices"][0]["delta"]["content"] == "hi"
    # [DONE] 之后不再产出
    assert len(payloads) == 1


# ---------------------------------------------------------------------------
# F20-4 仓库层四桶汇总
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> LibraryRepository:
    db_dir = tmp_path / "library"
    db_dir.mkdir(parents=True, exist_ok=True)
    factory = create_session_factory(db_dir / "tokens.db")
    init_database(factory)
    return LibraryRepository(factory)


def _insert_usage(repository: LibraryRepository, created_at: datetime, total: int, feature: str):
    from synthrecall.models import TokenUsageRecord

    factory_session = repository._session_factory()
    with factory_session as session:
        session.add(
            TokenUsageRecord(
                feature=feature,
                model="m",
                prompt_tokens=total - 1,
                completion_tokens=1,
                total_tokens=total,
                created_at=created_at,
            )
        )
        session.commit()


def test_token_usage_summary_buckets(tmp_path):
    repository = _make_repo(tmp_path)
    now = datetime.now(UTC).replace(tzinfo=None)
    _insert_usage(repository, now, 100, "AI 解析")
    _insert_usage(repository, now - timedelta(days=3), 200, "PDF 翻译")
    _insert_usage(repository, now - timedelta(days=10), 400, "PDF 翻译")
    _insert_usage(repository, now - timedelta(days=40), 800, "AI 解析")

    summary = repository.token_usage_summary()

    assert summary["today"]["total"] == 100
    assert summary["week"]["total"] == 300
    assert summary["month"]["total"] == 700
    assert summary["total"]["total"] == 1500
    assert summary["total"]["estimated"] == 0
    assert summary["features"] == {"AI 解析": 900, "PDF 翻译": 600}


def test_record_token_usage_never_raises(tmp_path):
    repository = _make_repo(tmp_path)
    repository.record_token_usage(
        feature="PDF 翻译",
        model="m",
        paper_id=None,
        source="babeldoc",
        prompt_tokens=1,
        completion_tokens=2,
        total_tokens=3,
        estimated=True,
    )
    summary = repository.token_usage_summary()
    assert summary["total"]["total"] == 3
    assert summary["total"]["estimated"] == 3


# ---------------------------------------------------------------------------
# F20-2 存储管理
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    (data_dir / "papers").mkdir(parents=True)
    (data_dir / "papers" / "a.pdf").write_bytes(b"x" * 4096)
    (data_dir / "translations" / "5").mkdir(parents=True)
    (data_dir / "translations" / "5" / "a.zh.mono.pdf").write_bytes(b"y" * 2048)
    working = data_dir / "translations" / "5" / "_working"
    working.mkdir()
    (working / "temp.bin").write_bytes(b"z" * 8192)
    (data_dir / "translations" / "orphan_dir").mkdir(parents=True)
    (data_dir / "translations" / "orphan_dir" / "old.mono.pdf").write_bytes(b"o" * 1024)
    (data_dir / ".url-imports").mkdir()
    (data_dir / ".url-imports" / "tmp.pdf").write_bytes(b"u" * 512)
    (data_dir / "babeldoc_home" / ".cache").mkdir(parents=True)
    (data_dir / "babeldoc_home" / ".cache" / "model.onnx").write_bytes(b"m" * 16384)
    (data_dir / "synthrecall_demo.db").write_bytes(b"d" * 32768)
    (data_dir / "backups").mkdir()
    (data_dir / "backups" / "b.zip").write_bytes(b"b" * 256)
    return data_dir


def test_scan_storage_categories(storage_dir: Path):
    from synthrecall.ui.storage_manager import scan_storage

    rows = {row.key: row for row in scan_storage(storage_dir)}
    assert rows["papers"].size == 4096
    assert rows["translation_pdfs"].size == 2048
    assert rows["working"].size == 8192
    assert rows["orphan_translations"].size == 1024
    assert rows["babeldoc"].size == 16384
    assert rows["db"].size == 32768
    assert rows["backups"].size == 256
    assert rows["url_imports"].size == 512
    # 可清理标记只落在派生物上
    assert rows["working"].deletable and rows["orphan_translations"].deletable
    assert not rows["db"].deletable and not rows["papers"].deletable
    assert not rows["babeldoc"].deletable


def test_storage_cleanup_helpers_touch_only_derived(storage_dir: Path):
    from synthrecall.ui.storage_manager import (
        find_orphan_translation_dirs,
        find_url_import_temp,
        find_working_dirs,
    )

    working_dirs = find_working_dirs(storage_dir)
    orphans = find_orphan_translation_dirs(storage_dir)
    temp_files = find_url_import_temp(storage_dir)
    assert len(working_dirs) == 1 and len(orphans) == 1 and len(temp_files) == 1

    import shutil

    for path in working_dirs + orphans:
        shutil.rmtree(path)
    for path in temp_files:
        path.unlink(missing_ok=True)

    assert find_working_dirs(storage_dir) == []
    assert find_orphan_translation_dirs(storage_dir) == []
    # 原文/数据库/缓存完好
    assert (storage_dir / "papers" / "a.pdf").exists()
    assert (storage_dir / "translations" / "5" / "a.zh.mono.pdf").exists()
    assert (storage_dir / "babeldoc_home" / ".cache" / "model.onnx").exists()


def test_storage_dialog_lists_and_cleans(storage_dir: Path, qtbot, monkeypatch):
    import synthrecall.ui.storage_manager as storage_module
    from synthrecall.ui.storage_manager import StorageManagerDialog

    monkeypatch.setattr(storage_module, "confirm_action", lambda *args, **kwargs: True)

    dialog = StorageManagerDialog(storage_dir)
    qtbot.addWidget(dialog)

    assert "总占用" in dialog.total_label.text()
    assert dialog.clean_working_button.isEnabled()
    assert dialog.clean_orphan_button.isEnabled()
    assert dialog.clean_url_import_button.isEnabled()

    dialog.clean_working_dirs()
    assert not (storage_dir / "translations" / "5" / "_working").exists()
    dialog.clean_orphan_translations()
    assert not (storage_dir / "translations" / "orphan_dir").exists()
    dialog.clean_url_imports()
    assert not (storage_dir / ".url-imports" / "tmp.pdf").exists()
    # 清理后按钮禁用(没有可清项)
    assert not dialog.clean_working_button.isEnabled()
    # 译文成品与原文仍在
    assert (storage_dir / "translations" / "5" / "a.zh.mono.pdf").exists()
    assert (storage_dir / "papers" / "a.pdf").exists()


# ---------------------------------------------------------------------------
# F20-4 统计页展示
# ---------------------------------------------------------------------------


def test_statistics_panel_shows_token_summary(tmp_path, qtbot):
    from synthrecall.ui.statistics_panel import StatisticsDialog

    repository = _make_repo(tmp_path)
    now = datetime.now(UTC).replace(tzinfo=None)
    _insert_usage(repository, now, 12345, "AI 解析")

    dialog = StatisticsDialog(repository)
    qtbot.addWidget(dialog)
    text = dialog.token_summary_label.text()
    assert "今日" in text and "12,345" in text and "累计" in text

    empty_repository = _make_repo(tmp_path / "empty")
    empty_dialog = StatisticsDialog(empty_repository)
    qtbot.addWidget(empty_dialog)
    assert "还没有 AI 调用记录" in empty_dialog.token_summary_label.text()
