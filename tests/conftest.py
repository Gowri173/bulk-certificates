"""Pytest configuration, fixtures, and helpers for isolated test execution."""

from collections.abc import Generator
from pathlib import Path
from typing import Any, Dict, List
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database
from app.config import Settings, set_settings
from app.database import Base, get_db
from app.main import app
from app.storage.local import LocalStorage
from app.worker.queue import shutdown


@pytest.fixture(autouse=True)
def test_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Dict[str, Any], None, None]:
    """Provide an isolated environment for each test with temp SQLite DB and storage dir."""
    db_file = tmp_path / "test_cert.db"
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)

    db_url = f"sqlite:///{db_file.as_posix()}"

    test_settings = Settings(
        DATABASE_URL=db_url,
        STORAGE_DIR=str(storage_dir),
        PROCESSING_MODE="inline",
        WORKER_THREADS=1,
        BATCH_SIZE=10,
        LOG_LEVEL="DEBUG",
    )

    # Set settings globally
    set_settings(test_settings)
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("STORAGE_DIR", str(storage_dir))
    monkeypatch.setenv("PROCESSING_MODE", "inline")

    # Configure SQLite engine for tests
    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False},
        echo=False,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Patch database module engine and SessionLocal
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.worker.processor.SessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.worker.queue.SessionLocal", TestingSessionLocal)

    # Patch storage
    test_storage = LocalStorage(base_dir=storage_dir)
    monkeypatch.setattr("app.storage.local.get_storage", lambda base_dir=None: test_storage)
    monkeypatch.setattr("app.worker.processor.get_storage", lambda: test_storage)
    monkeypatch.setattr("app.api.jobs.get_storage", lambda: test_storage)
    monkeypatch.setattr("app.api.certificates.get_storage", lambda: test_storage)

    # Create all tables
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    yield {
        "settings": test_settings,
        "engine": engine,
        "SessionLocal": TestingSessionLocal,
        "storage_dir": storage_dir,
        "storage": test_storage,
    }

    # Teardown
    shutdown()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    set_settings(None)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Provide a TestClient instance for calling API endpoints."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_session(test_environment: Dict[str, Any]) -> Generator[Any, None, None]:
    """Provide a direct DB session for inspecting test state."""
    session = test_environment["SessionLocal"]()
    try:
        yield session
    finally:
        session.close()


def make_payload(
    n: int = 3,
    event_name: str = "Python Masterclass 2026",
    title: str = "Certificate of Mastery",
    issuer_name: str = "Test Academy",
    issued_on: str = "2026-10-07",
) -> Dict[str, Any]:
    """Helper to generate a valid bulk request payload with n recipients."""
    recipients: List[Dict[str, Any]] = [
        {
            "name": f"Recipient {i}",
            "email": f"recipient_{i}@example.com",
            "achievement": f"Grade {i}" if i % 2 == 0 else None,
        }
        for i in range(1, n + 1)
    ]
    return {
        "event_name": event_name,
        "title": title,
        "issuer_name": issuer_name,
        "issued_on": issued_on,
        "recipients": recipients,
    }
