from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from PySide6.QtWidgets import QLineEdit

from synthrecall.ui.main_window import LLMSettingsDialog


class _ModelCatalogHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, str]]] = []

    def do_GET(self) -> None:
        self.__class__.requests.append(
            (self.path, self.headers.get("Authorization", ""))
        )
        payload = json.dumps(
            {
                "object": "list",
                "data": [
                    {"id": "deepseek-pro"},
                    {"id": "deepseek-flash"},
                ],
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args) -> None:
        return


def test_api_dialog_fetches_and_selects_custom_models(tmp_path, qtbot, monkeypatch):
    _ModelCatalogHandler.requests.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelCatalogHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        "synthrecall.ui.dialogs.llm_settings.get_llm_api_key",
        lambda *_args, **_kwargs: "",
    )
    dialog = LLMSettingsDialog(Path(tmp_path) / "llm_settings.json")
    qtbot.addWidget(dialog)
    # 默认服务 = 小红书 Dots(api-key 风格);验证 Bearer 拉取需切到 DeepSeek 预设
    dialog.preset_combo.setCurrentIndex(1)  # DeepSeek Chat(bearer)
    dialog.base_url_edit.setText(f"http://127.0.0.1:{server.server_port}/v1")
    dialog.api_key_edit.setText("test-secret")

    try:
        dialog.fetch_available_models()
        qtbot.waitUntil(
            lambda: dialog.fetch_models_button.isEnabled()
            and dialog.model_combo.count() == 2,
            timeout=5_000,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert _ModelCatalogHandler.requests == [
        ("/v1/models", "Bearer test-secret")
    ]
    assert [
        dialog.model_combo.itemText(index)
        for index in range(dialog.model_combo.count())
    ] == ["deepseek-flash", "deepseek-pro"]
    dialog.model_combo.setCurrentText("deepseek-pro")
    assert dialog.model_edit.text() == "deepseek-pro"


def test_api_dialog_switches_cached_keys_and_toggles_visibility(
    tmp_path, qtbot, monkeypatch
):
    keys = {
        "https://api.deepseek.com": "deepseek-key",
        "https://note3-prev-api.askdiandian.com/v1": "dots-key",
    }
    monkeypatch.setattr(
        "synthrecall.ui.dialogs.llm_settings.get_llm_api_key",
        lambda base_url="", **_kwargs: keys.get(base_url.rstrip("/"), ""),
    )
    dialog = LLMSettingsDialog(Path(tmp_path) / "llm_settings.json")
    qtbot.addWidget(dialog)

    # 新默认服务 = 小红书 Dots(索引 0);切到 DeepSeek(索引 1)验证 key 缓存切换
    assert dialog.api_key_edit.text() == "dots-key"
    dialog.preset_combo.setCurrentIndex(1)
    assert dialog.api_key_edit.text() == "deepseek-key"
    dialog.api_key_edit.setText("edited-deepseek-key")
    dialog.preset_combo.setCurrentIndex(0)
    assert dialog.api_key_edit.text() == "dots-key"
    dialog.preset_combo.setCurrentIndex(1)
    assert dialog.api_key_edit.text() == "edited-deepseek-key"

    assert dialog.api_key_edit.echoMode() == QLineEdit.EchoMode.Password
    dialog.api_key_visibility_button.click()
    assert dialog.api_key_edit.echoMode() == QLineEdit.EchoMode.Normal
    dialog.api_key_visibility_button.click()
    assert dialog.api_key_edit.echoMode() == QLineEdit.EchoMode.Password
