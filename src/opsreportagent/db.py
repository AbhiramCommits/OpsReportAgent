"""SQLAlchemy 2.x engine/session factory and ORM models.

Works against PostgreSQL (production) or SQLite (local/demo) purely by changing
``DATABASE_URL``. Every table in the ops-reporting data model lives here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from .config import Settings, load_settings


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_path: Mapped[str] = mapped_column(String(512), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    extractions: Mapped[list[Extraction]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Document id={self.id} type={self.doc_type} path={self.source_path!r}>"


class Extraction(Base):
    __tablename__ = "extractions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(64))
    subcategory: Mapped[str | None] = mapped_column(String(64))
    store_id: Mapped[str | None] = mapped_column(String(32))
    incident_date: Mapped[str | None] = mapped_column(String(32))
    vendor: Mapped[str | None] = mapped_column(String(128))
    dollar_impact: Mapped[float | None] = mapped_column(Float)
    severity: Mapped[str | None] = mapped_column(String(32))
    summary: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prompt_version: Mapped[str] = mapped_column(String(16), nullable=False, default="v1")
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="stub")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[Document] = relationship(back_populates="extractions")
    reviews: Mapped[list[Review]] = relationship(
        back_populates="extraction", cascade="all, delete-orphan"
    )

    # Fields that reviewers may correct and that are scored against gold labels.
    CORRECTABLE_FIELDS = (
        "category",
        "subcategory",
        "store_id",
        "incident_date",
        "vendor",
        "dollar_impact",
        "severity",
        "summary",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Extraction id={self.id} run={self.run_id} category={self.category!r}>"


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    extraction_id: Mapped[int] = mapped_column(
        ForeignKey("extractions.id"), nullable=False, index=True
    )
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    extraction: Mapped[Extraction] = relationship(back_populates="reviews")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Review id={self.id} field={self.field_name} by={self.reviewer!r}>"


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    doc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="stub")
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="stub")
    prompt_version: Mapped[str] = mapped_column(String(16), nullable=False, default="v1")
    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Run run_id={self.run_id!r} docs={self.doc_count}>"


class Score(Base):
    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exact_match: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accuracy: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Score run={self.run_id} field={self.field_name} acc={self.accuracy:.3f}>"


class Database:
    """Owns an engine + session factory for one database URL."""

    def __init__(self, url: str):
        self.url = url
        # check_same_thread is a SQLite-only flag.
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, future=True, connect_args=connect_args)
        self.SessionLocal = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False, class_=Session
        )

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        Base.metadata.drop_all(self.engine)

    def table_names(self) -> list[str]:
        return sorted(inspect(self.engine).get_table_names())

    def session(self) -> Session:
        return self.SessionLocal()

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


_DEFAULT: Database | None = None


def get_database(url: str | None = None, settings: Settings | None = None) -> Database:
    """Return a :class:`Database`, caching the default (env-configured) one."""
    global _DEFAULT
    if url is None and settings is not None:
        url = settings.database_url
    if url is None:
        if _DEFAULT is None:
            _DEFAULT = Database(load_settings().database_url)
        return _DEFAULT
    return Database(url)


def reset_default_database() -> None:
    """Testing helper: forget the cached default database."""
    global _DEFAULT
    _DEFAULT = None


def init_db(url: str | None = None) -> list[str]:
    """Create all tables and return the resulting table list."""
    db = get_database(url)
    db.create_all()
    return db.table_names()
