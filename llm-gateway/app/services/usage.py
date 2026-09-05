"""SQLite persistence for completed gateway-call observability records."""
import logging
import sqlite3
from pathlib import Path

from app.data import UsageQuery, UsageRecord


logger = logging.getLogger(__name__)


class UsageRepository:
    """封装 usage_events 表的初始化、终态写入和受限查询。"""

    def __init__(self, database_path: str) -> None:
        self._database_path = database_path

    def record(self, record: UsageRecord) -> None:
        """原子写入一条终态记录；相同 request_id 不会重复插入。"""
        with self._connection() as database:
            self._initialize(database)
            database.execute(
                """INSERT OR REPLACE INTO usage_events (
                    request_id, caller_id_hash, model, status, input_tokens, output_tokens, total_tokens,
                    latency_ms, first_token_latency_ms, retry_count, error_type, error_code,
                    prompt_id, prompt_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.request_id, record.caller_id_hash, record.model, record.status,
                    record.input_tokens, record.output_tokens, record.total_tokens,
                    record.latency_ms, record.first_token_latency_ms, record.retry_count,
                    record.error_type, record.error_code, record.prompt_id, record.prompt_version,
                    record.created_at.isoformat(), record.updated_at.isoformat(),
                ),
            )

    def record_safely(self, record: UsageRecord) -> None:
        """观测写入失败只记日志，绝不改变模型调用的成功或失败语义。"""
        try:
            self.record(record)
        except Exception:
            logger.exception("Failed to persist usage record request_id=%s", record.request_id)

    def list(self, query: UsageQuery) -> list[UsageRecord]:
        """按白名单条件查询历史记录，不返回任何敏感请求内容。"""
        clauses: list[str] = []
        values: list[object] = []
        if query.model is not None:
            clauses.append("model = ?")
            values.append(query.model)
        if query.status is not None:
            clauses.append("status = ?")
            values.append(query.status)
        if query.request_id is not None:
            clauses.append("request_id = ?")
            values.append(query.request_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as database:
            self._initialize(database)
            rows = database.execute(
                f"SELECT * FROM usage_events{where} ORDER BY created_at DESC LIMIT ?", (*values, query.limit)
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def _connection(self) -> sqlite3.Connection:
        database_path = Path(self._database_path)
        if database_path.parent != Path("."):
            database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _initialize(database: sqlite3.Connection) -> None:
        database.execute(
            """CREATE TABLE IF NOT EXISTS usage_events (
                request_id TEXT PRIMARY KEY,
                caller_id_hash TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                input_tokens INTEGER NULL,
                output_tokens INTEGER NULL,
                total_tokens INTEGER NULL,
                latency_ms REAL NOT NULL,
                first_token_latency_ms REAL NULL,
                retry_count INTEGER NOT NULL,
                error_type TEXT NULL,
                error_code TEXT NULL,
                prompt_id TEXT NULL,
                prompt_version INTEGER NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        database.execute("CREATE INDEX IF NOT EXISTS idx_usage_created_at ON usage_events(created_at DESC)")
        database.execute("CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_events(model)")
        database.execute("CREATE INDEX IF NOT EXISTS idx_usage_status ON usage_events(status)")

    @staticmethod
    def _to_record(row: sqlite3.Row) -> UsageRecord:
        return UsageRecord(
            request_id=row["request_id"], caller_id_hash=row["caller_id_hash"], model=row["model"], status=row["status"],
            input_tokens=row["input_tokens"], output_tokens=row["output_tokens"], total_tokens=row["total_tokens"],
            latency_ms=row["latency_ms"], first_token_latency_ms=row["first_token_latency_ms"],
            retry_count=row["retry_count"], error_type=row["error_type"], error_code=row["error_code"],
            prompt_id=row["prompt_id"], prompt_version=row["prompt_version"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )
