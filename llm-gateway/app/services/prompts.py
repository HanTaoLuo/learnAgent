"""Versioned, sandboxed Jinja2 prompt templates backed by SQLite."""
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from jinja2 import StrictUndefined, TemplateError, UndefinedError
from jinja2.exceptions import SecurityError, TemplateSyntaxError
from jinja2.sandbox import SandboxedEnvironment

from app.data import ModelMessage, ModelRequest
from app.data.prompt import PromptReference, PromptTemplate, PromptUse, PromptVersionCreate
from app.errors import InvalidRequestError, ResourceNotFoundError


class PromptService:
    """管理提示词版本、Jinja2 安全渲染及对模型请求的注入。"""

    def __init__(self, database_path: str) -> None:
        self._database_path = database_path
        self._environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)

    def create_version(self, payload: PromptVersionCreate) -> PromptTemplate:
        """创建新提示词或原子地追加一个递增版本。"""
        prompt_id = payload.id or f"prompt_{uuid4().hex}"
        with self._connection() as database:
            self._initialize(database)
            row = database.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM prompt_versions WHERE prompt_id = ?", (prompt_id,)
            ).fetchone()
            version = int(row["version"]) + 1
            active = payload.activate or version == 1
            if active:
                database.execute("UPDATE prompt_versions SET active = 0 WHERE prompt_id = ?", (prompt_id,))
            created_at = datetime.now(UTC)
            database.execute(
                """INSERT INTO prompt_versions(prompt_id, version, template, role, active, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (prompt_id, version, payload.template, payload.role, int(active), created_at.isoformat()),
            )
        return PromptTemplate(
            id=prompt_id, version=version, template=payload.template, role=payload.role, active=active, created_at=created_at
        )

    def get(self, prompt_id: str, version: int | None = None) -> PromptTemplate:
        """按版本获取模板；省略版本时获取当前 active version。"""
        with self._connection() as database:
            self._initialize(database)
            if version is None:
                row = database.execute(
                    "SELECT * FROM prompt_versions WHERE prompt_id = ? AND active = 1", (prompt_id,)
                ).fetchone()
            else:
                row = database.execute(
                    "SELECT * FROM prompt_versions WHERE prompt_id = ? AND version = ?", (prompt_id, version)
                ).fetchone()
        if row is None:
            suffix = "active version" if version is None else f"version {version}"
            raise ResourceNotFoundError(f"Prompt {prompt_id!r} {suffix} was not found", code="prompt_not_found")
        return self._to_template(row)

    def list(self, prompt_id: str | None = None) -> list[PromptTemplate]:
        """列出全部提示词版本，或指定提示词的版本历史。"""
        with self._connection() as database:
            self._initialize(database)
            if prompt_id is None:
                rows = database.execute("SELECT * FROM prompt_versions ORDER BY prompt_id, version DESC").fetchall()
            else:
                rows = database.execute(
                    "SELECT * FROM prompt_versions WHERE prompt_id = ? ORDER BY version DESC", (prompt_id,)
                ).fetchall()
        return [self._to_template(row) for row in rows]

    def activate(self, prompt_id: str, version: int) -> PromptTemplate:
        """将一个存在的版本设为唯一 active version。"""
        with self._connection() as database:
            self._initialize(database)
            row = database.execute(
                "SELECT 1 FROM prompt_versions WHERE prompt_id = ? AND version = ?", (prompt_id, version)
            ).fetchone()
            if row is None:
                raise ResourceNotFoundError(f"Prompt {prompt_id!r} version {version} was not found", code="prompt_not_found")
            database.execute("UPDATE prompt_versions SET active = 0 WHERE prompt_id = ?", (prompt_id,))
            database.execute(
                "UPDATE prompt_versions SET active = 1 WHERE prompt_id = ? AND version = ?", (prompt_id, version)
            )
        return self.get(prompt_id, version)

    def render(self, reference: PromptReference) -> tuple[PromptTemplate, str]:
        """安全渲染引用版本；模板正文与变量不会被持久化或记录。"""
        template = self.get(reference.id, reference.version)
        try:
            return template, self._environment.from_string(template.template).render(**reference.variables)
        except UndefinedError as exc:
            raise InvalidRequestError(f"Prompt variable is required: {exc}", code="prompt_variable_missing") from exc
        except (TemplateSyntaxError, SecurityError, TemplateError) as exc:
            raise InvalidRequestError(f"Invalid prompt template: {exc}", code="prompt_template_invalid") from exc

    def apply(self, request: ModelRequest) -> tuple[ModelRequest, PromptUse | None]:
        """渲染并注入提示词，返回新的请求和可用于审计的非敏感元数据。"""
        if request.prompt is None:
            return request, None
        template, rendered = self.render(request.prompt)
        reference = request.prompt
        if reference.position == "system":
            system = "\n\n".join(part for part in (rendered, request.system) if part) or None
            updated = request.model_copy(update={"system": system})
        else:
            message = ModelMessage(role=template.role, content=rendered)
            messages = list(request.messages)
            if reference.position == "prepend_messages":
                messages.insert(0, message)
            else:
                messages.append(message)
            updated = request.model_copy(update={"messages": messages})
        return updated, PromptUse(id=template.id, version=template.version)

    def _connection(self) -> sqlite3.Connection:
        """打开具备事务提交语义的 SQLite 连接。"""
        database_path = Path(self._database_path)
        if database_path.parent != Path("."):
            database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _initialize(database: sqlite3.Connection) -> None:
        database.execute(
            """CREATE TABLE IF NOT EXISTS prompt_versions (
                prompt_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                template TEXT NOT NULL,
                role TEXT NOT NULL,
                active INTEGER NOT NULL CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL,
                PRIMARY KEY (prompt_id, version)
            )"""
        )
        database.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_prompt_active ON prompt_versions(prompt_id) WHERE active = 1"
        )

    @staticmethod
    def _to_template(row: sqlite3.Row) -> PromptTemplate:
        return PromptTemplate(
            id=row["prompt_id"], version=row["version"], template=row["template"], role=row["role"],
            active=bool(row["active"]), created_at=datetime.fromisoformat(row["created_at"]),
        )
