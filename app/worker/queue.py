"""Background job queue with ThreadPoolExecutor and application startup recovery."""

from concurrent.futures import ThreadPoolExecutor
import logging
from typing import Optional
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.enums import JobStatus
from app.models import Job
from app.worker.processor import process_job

logger = logging.getLogger(__name__)

_executor: Optional[ThreadPoolExecutor] = None


def get_executor() -> ThreadPoolExecutor:
    """Return the active ThreadPoolExecutor, initializing it lazily if needed."""
    global _executor
    if _executor is None:
        settings = get_settings()
        _executor = ThreadPoolExecutor(
            max_workers=settings.WORKER_THREADS,
            thread_name_prefix="cert_worker",
        )
    return _executor


def enqueue(job_id: str) -> None:
    """Enqueue a job for processing.

    In inline mode (e.g. during testing), execution runs synchronously.
    In background mode, execution is offloaded to the thread pool.
    """
    settings = get_settings()
    if settings.PROCESSING_MODE == "inline":
        logger.debug("Processing job %s synchronously (PROCESSING_MODE=inline)", job_id)
        process_job(job_id)
    else:
        logger.info("Enqueuing job %s for background processing", job_id)
        executor = get_executor()
        executor.submit(process_job, job_id)


def recover_incomplete_jobs() -> None:
    """Find incomplete or interrupted jobs on application startup and re-enqueue them."""
    db: Session = SessionLocal()
    try:
        incomplete_jobs = (
            db.query(Job)
            .filter(
                Job.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]),
                Job.completed_at.is_(None),
            )
            .all()
        )

        if incomplete_jobs:
            logger.info("Startup crash recovery: found %d incomplete jobs", len(incomplete_jobs))
            for job in incomplete_jobs:
                logger.info("Recovering incomplete job %s (status=%s)", job.id, job.status.value)
                enqueue(job.id)
    except Exception as exc:
        logger.exception("Error recovering incomplete jobs during startup: %s", exc)
    finally:
        db.close()


def shutdown() -> None:
    """Gracefully terminate background worker threads."""
    global _executor
    if _executor is not None:
        logger.info("Shutting down worker thread pool...")
        _executor.shutdown(wait=True)
        _executor = None
