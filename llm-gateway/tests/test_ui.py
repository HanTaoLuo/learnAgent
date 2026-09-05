import asyncio

from fastapi.testclient import TestClient

from app.adapters import ModelRegistry
from app.config import DEEPSEEK_API_KEY_ENV, UiSettings
from app.data import ModelCapability
from app.main import app
from app.services.runtime_config import RuntimeKeyService
from app.services.ui import open_browser_safely, should_auto_open_browser


def test_local_management_page_has_four_tabs_and_no_browser_storage() -> None:
    with TestClient(app) as client:
        response = client.get("/")
        script = client.get("/static/app.js")

    assert response.status_code == 200
    assert "Gateway" in response.text
    assert "调用调试" in response.text
    assert "Prompts" in response.text
    assert "用量监控" in response.text
    assert "重新订阅遗漏内容" in response.text
    assert "取消生成" in response.text
    assert "新建对话" in response.text
    assert "localStorage" not in script.text
    assert 'fetch("/v1/generate"' in script.text
    assert "activeConversationId" in script.text
    assert "当前聊天窗口的成功历史也会自动透传" in script.text
    assert "assistantMessage.content = pretty(await response.json())" not in script.text
    assert "enqueueTypewriter" in script.text
    assert "/v1/streams/" in script.text
    assert "activeSubscription" in script.text
    assert "模型输出已接收完，正在本地逐字展示" in script.text
    assert "flushTypewriter" in script.text
    assert "已接收文字已立即显示" in script.text
    assert "followOutput" in script.text
    assert "你没有配置 API key，请在第一个页面配置后进行对话。" in script.text
    assert 'class="request-settings" open' not in response.text
    assert 'addMessage("user", "你好")' not in script.text
    assert '$("user-input").value=""' in script.text


def test_multi_turn_ui_includes_visible_conversation_history_in_request_payload() -> None:
    with TestClient(app) as client:
        script = client.get("/static/app.js")

    assert "function currentConversationHistory" in script.text
    assert "buildMultiTurnMessages" in script.text


def test_runtime_key_is_not_in_initial_config_and_requires_explicit_environment_read(monkeypatch) -> None:
    class RuntimeAdapter:
        def __init__(self) -> None:
            self.api_key_env = DEEPSEEK_API_KEY_ENV
            self.api_key = ""
            self.refreshes = 0

        def set_runtime_api_key(self, api_key: str) -> None:
            self.api_key = api_key
            self.refreshes += 1

    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "environment-key")
    with TestClient(app) as client:
        registry = ModelRegistry()
        adapter = RuntimeAdapter()
        registry.register("local-model", adapter, ModelCapability(True, True, "prompt_only", False, True, True))  # type: ignore[arg-type]
        client.app.state.model_registry = registry
        client.app.state.runtime_key_service = RuntimeKeyService(registry)
        config = client.get("/admin/ui/config")
        response = client.post("/admin/ui/runtime-key/load-environment")

    assert config.status_code == 200
    assert config.json()["deepseek_key"] == {"source": "environment", "configured": True}
    assert "value" not in config.json()["deepseek_key"]
    assert response.status_code == 200
    assert response.json() == {"source": "environment", "configured": True, "value": "environment-key"}
    assert adapter.api_key == "environment-key"
    assert adapter.refreshes == 1


def test_browser_auto_open_respects_disabled_and_ci_settings(monkeypatch) -> None:
    assert should_auto_open_browser(UiSettings(auto_open=False)) is False
    monkeypatch.setenv("CI", "1")
    assert should_auto_open_browser(UiSettings(auto_open=True)) is False
    asyncio.run(open_browser_safely(UiSettings(auto_open=False)))


def test_ui_typewriter_settings_are_loaded_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_UI_TYPEWRITER_INTERVAL_MS", "48")
    monkeypatch.setenv("GATEWAY_UI_TYPEWRITER_CHARS_PER_TICK", "2")

    settings = UiSettings.from_env()

    assert settings.typewriter_interval_ms == 48
    assert settings.typewriter_chars_per_tick == 2
