import pytest

from app.data import ModelMessage, ModelRequest, PromptReference, PromptVersionCreate
from app.errors import InvalidRequestError
from app.services.prompts import PromptService


def test_prompt_versions_activate_and_resolve_active_version(tmp_path) -> None:
    service = PromptService(str(tmp_path / "gateway.db"))
    first = service.create_version(PromptVersionCreate(id="support", template="v1", activate=True))
    second = service.create_version(PromptVersionCreate(id="support", template="v2", activate=False))

    assert first.version == 1
    assert second.version == 2
    assert service.get("support").version == 1
    assert service.activate("support", 2).active is True
    assert service.get("support").version == 2


def test_prompt_renders_jinja_control_flow_with_strict_variables(tmp_path) -> None:
    service = PromptService(str(tmp_path / "gateway.db"))
    service.create_version(
        PromptVersionCreate(
            id="summary",
            template="{% for item in items %}[{{ item }}]{% endfor %}{% if urgent %}!{% endif %}",
        )
    )

    _, rendered = service.render(PromptReference(id="summary", variables={"items": ["a", "b"], "urgent": True}))
    assert rendered == "[a][b]!"


def test_prompt_missing_variable_and_invalid_template_are_invalid_requests(tmp_path) -> None:
    service = PromptService(str(tmp_path / "gateway.db"))
    service.create_version(PromptVersionCreate(id="missing", template="Hello {{ name }}"))
    service.create_version(PromptVersionCreate(id="broken", template="{% if %}"))

    with pytest.raises(InvalidRequestError, match="Prompt variable is required") as missing:
        service.render(PromptReference(id="missing"))
    assert missing.value.code == "prompt_variable_missing"
    with pytest.raises(InvalidRequestError, match="Invalid prompt template") as broken:
        service.render(PromptReference(id="broken"))
    assert broken.value.code == "prompt_template_invalid"


def test_prompt_sandbox_rejects_unsafe_attribute_access(tmp_path) -> None:
    service = PromptService(str(tmp_path / "gateway.db"))
    service.create_version(PromptVersionCreate(id="unsafe", template="{{ ''.__class__.__mro__ }}"))

    with pytest.raises(InvalidRequestError) as error:
        service.render(PromptReference(id="unsafe"))
    assert error.value.code == "prompt_template_invalid"


def test_prompt_apply_injects_system_and_message_positions(tmp_path) -> None:
    service = PromptService(str(tmp_path / "gateway.db"))
    service.create_version(PromptVersionCreate(id="system", template="Policy: {{ policy }}", role="system"))
    service.create_version(PromptVersionCreate(id="prepend", template="context", role="user"))
    service.create_version(PromptVersionCreate(id="append", template="ack", role="assistant"))
    base = ModelRequest(model="deepseek-v4-pro", system="existing", messages=[ModelMessage(role="user", content="hello")])

    system, system_use = service.apply(base.model_copy(update={"prompt": PromptReference(id="system", variables={"policy": "safe"})}))
    prepend, _ = service.apply(base.model_copy(update={"prompt": PromptReference(id="prepend", position="prepend_messages")}))
    append, _ = service.apply(base.model_copy(update={"prompt": PromptReference(id="append", position="append_messages")}))

    assert system.system == "Policy: safe\n\nexisting"
    assert system_use is not None and system_use.version == 1
    assert prepend.messages[0].content == "context"
    assert append.messages[-1].content == "ack"
