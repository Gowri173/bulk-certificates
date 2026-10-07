# Bulk Certificate Generator (`bulk-certificates`)

A production-grade, resilient backend service built with FastAPI, SQLAlchemy 2.0, and ReportLab to generate PDF certificates from bulk requests. It features asynchronous background processing, per-certificate failure isolation, two-level validation, idempotency, startup crash recovery, and single-PDF/ZIP archive downloads.

---

## 1. Overview

Organizations hosting workshops, bootcamps, and hackathons often need to generate hundreds or thousands of personalized PDF certificates at once. Generating them synchronously during a single HTTP request causes proxy timeouts, worker starvation, and lost progress on network disconnection.

`bulk-certificates` solves this by:
1. Accepting bulk submissions asynchronously via `POST /api/v1/certificate-jobs` (returning `202 Accepted`).
2. Storing the job and each recipient row in a relational database as the single source of truth.
3. Processing certificate generation in the background with an in-process thread pool.
4. Isolating errors so a failure on one recipient never cancels or crashes the rest of the batch.
5. Providing endpoints to poll real-time progress, inspect individual successes/failures, download single certificates, or stream a consolidated ZIP archive.

---

## 2. Tech Stack

- **Python**: 3.10+ / 3.11+
- **API Framework**: FastAPI 0.110+ with Uvicorn
- **ORM & Database**: SQLAlchemy 2.0 (typed `Mapped[]` declarative models), SQLite (default) and PostgreSQL-compatible via `DATABASE_URL`
- **Validation & Settings**: Pydantic v2, `pydantic-settings`, `email-validator`
- **PDF Generation Engine**: ReportLab 4.1+ (pure Python, deterministic layout, no external C/HTML dependencies)
- **Testing**: pytest 8+, httpx (TestClient), pypdf (PDF text inspection)
- **Concurrency**: In-process `ThreadPoolExecutor` worker with crash recovery

---

## 3. Project Structure

```
bulk-certificates/
├── app/
│   ├── main.py                 # FastAPI application factory, lifespan hooks, and exception handlers
│   ├── config.py               # Typed configuration via pydantic-settings
│   ├── database.py             # Engine, SessionLocal, Base, and get_db dependency
│   ├── models.py               # SQLAlchemy 2.0 ORM models (Job, Certificate)
│   ├── schemas.py              # Pydantic v2 request & response schemas
│   ├── enums.py                # JobStatus, CertificateStatus, ErrorCode enums
│   ├── api/
│   │   ├── jobs.py             # Job creation, status, listing, retry, and ZIP download endpoints
│   │   └── certificates.py     # Single certificate PDF download endpoint
│   ├── services/
│   │   ├── job_service.py      # Job persistence, queries, summary computations, retry logic
│   │   ├── validation.py       # Level 2 recipient validator (pure function)
│   │   └── zip_service.py      # Non-blocking temporary file ZIP archive builder
│   ├── worker/
│   │   ├── queue.py            # ThreadPoolExecutor queue, enqueue(), and startup crash recovery
│   │   └── processor.py        # process_job() with atomic claims and per-certificate commits
│   ├── generator/
│   │   ├── base.py             # CertificateData dataclass and CertificateGenerator protocol
│   │   └── pdf.py              # ReportLab A4 landscape certificate renderer
│   └── storage/
│       ├── base.py             # Storage protocol (save, open_path, exists)
│       └── local.py            # Local filesystem storage with directory traversal protection
├── tests/
│   ├── conftest.py             # Pytest fixtures (temp SQLite, temp storage, inline mode, client)
│   ├── test_create_job.py      # Job submission, status codes, and idempotency tests
│   ├── test_validation.py      # Level 1 and Level 2 validation tests
│   ├── test_generation.py      # ReportLab rendering, PDF header, pypdf text, and serial uniqueness
│   ├── test_job_status.py      # Lifecycle transitions, progress calculation, crash recovery, and pagination
│   ├── test_failure_isolation.py # Monkeypatched generator failure isolation and retry tests
│   └── test_retrieval.py       # Single PDF download, 409 conflict checks, and ZIP download tests
├── requirements.txt
├── .env.example
├── pytest.ini
├── README.md
└── .gitignore
```

---

## 4. Setup & Installation

### Step 1: Clone and create virtual environment
```bash
# Clone the repository
git clone <repo-url>
cd "Bulk Certificate Generator"

# Create a virtual environment
python -m venv .venv

# Activate virtual environment
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate
```

### Step 2: Install dependencies
```bash
pip install -r requirements.txt
```

### Step 3: Configure environment variables
```bash
# Copy template configuration
cp .env.example .env
```

---

## 5. Running the Application

Start the development server with hot reload:
```bash
uvicorn app.main:app --reload --port 8000
```

Once running:
- **Interactive OpenAPI Documentation**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc Documentation**: [http://localhost:8000/redoc](http://localhost:8000/redoc)
- **Health Check**: [http://localhost:8000/health](http://localhost:8000/health)

---

## 6. Running Tests

Run the full automated test suite using `pytest`:
```bash
pytest
```

To run with verbose output:
```bash
pytest -v
```

---

## 7. API Usage Examples (`curl`)

### 7.1 Submit a Bulk Certificate Job
Submit a batch containing 3 recipients (2 valid, 1 invalid to demonstrate Level 2 validation):

```bash
curl -X POST http://localhost:8000/api/v1/certificate-jobs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: batch-2026-001" \
  -d '{
    "event_name": "Full-Stack Python Bootcamp 2026",
    "title": "Certificate of Completion",
    "issuer_name": "Acme Academy",
    "issued_on": "2026-10-07",
    "recipients": [
      {
        "name": "Asha Rao",
        "email": "asha@example.com",
        "achievement": "With Distinction"
      },
      {
        "name": "Ravi Kumar",
        "email": "ravi@example.com"
      },
      {
        "name": "",
        "email": "invalid-email-format"
      }
    ]
  }'
```

**Response (`202 Accepted`):**
```json
{
  "job_id": "c16fa05d-85f2-4bc4-b788-df3d7e523315",
  "status": "pending",
  "total_count": 3,
  "accepted_count": 2,
  "rejected_count": 1,
  "links": {
    "self": "/api/v1/certificate-jobs/c16fa05d-85f2-4bc4-b788-df3d7e523315",
    "certificates": "/api/v1/certificate-jobs/c16fa05d-85f2-4bc4-b788-df3d7e523315/certificates",
    "download_all": "/api/v1/certificate-jobs/c16fa05d-85f2-4bc4-b788-df3d7e523315/download"
  }
}
```

---

### 7.2 Check Job Status and Progress

```bash
curl -X GET http://localhost:8000/api/v1/certificate-jobs/c16fa05d-85f2-4bc4-b788-df3d7e523315
```

**Response (`200 OK`):**
```json
{
  "job_id": "c16fa05d-85f2-4bc4-b788-df3d7e523315",
  "status": "completed_with_errors",
  "event_name": "Full-Stack Python Bootcamp 2026",
  "total_count": 3,
  "success_count": 2,
  "failed_count": 1,
  "pending_count": 0,
  "progress_percent": 100.0,
  "created_at": "2026-10-07T09:50:00.123456Z",
  "started_at": "2026-10-07T09:50:00.234567Z",
  "completed_at": "2026-10-07T09:50:01.345678Z"
}
```

---

### 7.3 List Certificates with Filters

**List all certificates:**
```bash
curl -X GET "http://localhost:8000/api/v1/certificate-jobs/c16fa05d-85f2-4bc4-b788-df3d7e523315/certificates?limit=50&offset=0"
```

**Filter only failed certificates:**
```bash
curl -X GET "http://localhost:8000/api/v1/certificate-jobs/c16fa05d-85f2-4bc4-b788-df3d7e523315/certificates?status=failed"
```

**Response (`200 OK`):**
```json
{
  "total": 1,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "id": "e0b12ad3-4c9f-4351-8201-44754bba8999",
      "row_index": 2,
      "recipient_name": "",
      "recipient_email": "invalid-email-format",
      "status": "failed",
      "serial_number": null,
      "download_url": null,
      "error_code": "VALIDATION_ERROR",
      "error_message": "name: must not be empty; email: invalid format"
    }
  ]
}
```

---

### 7.4 Download a Single Certificate PDF

```bash
curl -o certificate.pdf -X GET http://localhost:8000/api/v1/certificates/76a4b123-11aa-4bb2-901a-821938ab0123/download
```

---

### 7.5 Download All Successful Certificates as a ZIP Archive

```bash
curl -o certificates.zip -X GET http://localhost:8000/api/v1/certificate-jobs/c16fa05d-85f2-4bc4-b788-df3d7e523315/download
```

---

### 7.6 Retry Failed Certificate Generations

```bash
curl -X POST http://localhost:8000/api/v1/certificate-jobs/c16fa05d-85f2-4bc4-b788-df3d7e523315/retry-failed
```

---

## 8. Key Architectural & Design Decisions

### 8.1 Synchronous API vs. Asynchronous Background Worker
- **Decision**: Accept the job, persist it immediately, enqueue background processing, and return `202 Accepted` with a tracking ID.
- **Rationale**: Generating thousands of vector/PDF documents requires seconds or minutes of CPU time. Synchronous handling would block HTTP worker threads, exceed reverse proxy timeouts (e.g. Nginx 60s timeout), and lose all work if the network connection breaks midway.

### 8.2 Database as Single Source of Truth
- **Decision**: In-memory job queues only carry `job_id` strings. All state transitions, progress counters, file references, and error logs are stored in the database.
- **Rationale**: If the application crashes or restarts, memory state is lost, but the database preserves which rows succeeded, which failed, and which remain pending.

### 8.3 Per-Certificate Commit and Failure Isolation
- **Decision**: In `app/worker/processor.py`, each certificate is claimed atomically (`UPDATE certificates SET status='processing' WHERE id=:id AND status='pending'`), generated inside an isolated `try/except` block, and committed individually to the database.
- **Rationale**: A rendering error or malformed input on row 42 does not fail or rollback rows 1..41 or block rows 43..1000. Progress is continuous and durable.

### 8.4 Two-Level Validation Strategy
- **Level 1 (Request-Level, HTTP 422)**: Validates overall payload structure (missing `recipients` array, empty list, exceeding `MAX_RECIPIENTS`, missing `event_name`, invalid date format).
- **Level 2 (Recipient-Level, Stored as `failed`)**: Each recipient row is evaluated independently in `validation.py`. Invalid rows are assigned `status=failed`, `error_code=VALIDATION_ERROR`, and a human-readable composite `error_message`. The overall job is still accepted so valid recipients can be generated without friction.

### 8.5 Why Failed Rows are Stored
- **Decision**: Both valid and invalid recipients are assigned rows in the `certificates` table with their original `row_index`.
- **Rationale**: Gives clients a 1:1 mapped audit trail. Callers can query `GET /certificates?status=failed` to identify precisely which submitted rows failed and why, correct the data, and submit only the fixes.

### 8.6 Idempotency
- **Decision**: Clients can supply an `Idempotency-Key` header. If a job with that key already exists, the server returns the existing job instead of generating duplicates.
- **Rationale**: Protects against network retries creating duplicated certificate records or regenerating existing files.

### 8.7 Crash Recovery on Startup
- **Decision**: During FastAPI lifespan startup (`app/main.py`), `recover_incomplete_jobs()` queries all jobs in `pending` or `processing` status without a `completed_at` timestamp and enqueues them. Any certificate rows left in `processing` by an interrupted worker are reset to `pending` and resumed.
- **Rationale**: Zero operator intervention needed after an ungraceful container or process restart.

### 8.8 Storage Abstraction
- **Decision**: Storage operations are defined by the `Storage` protocol (`save`, `open_path`, `exists`). The `LocalStorage` implementation enforces strict path normalization and verifies that resolved paths cannot escape `STORAGE_DIR` (guarding against directory traversal attacks).
- **Rationale**: Enables swapping local disk for AWS S3, Google Cloud Storage, or Azure Blob Storage by changing one file without touching worker or router logic.

---

## 9. Known Limitations & Production Scaling Path

| Dimension | Current Architecture (Assessment Setup) | Production Scaling Path |
|---|---|---|
| **Worker Queue** | In-process `ThreadPoolExecutor` | Distributed task queue: **Celery**, **RQ**, or **Temporal** backed by Redis/RabbitMQ. |
| **Database** | SQLite with WAL mode & `check_same_thread=False` | Managed **PostgreSQL** with connection pooling (e.g. PgBouncer / SQLAlchemy asyncpg). |
| **Storage** | Local disk directory (`STORAGE_DIR`) | Object Storage (**AWS S3**, **GCS**, **Cloudflare R2**) using pre-signed download URLs. |
| **Typography / I18N** | Built-in Helvetica/Times (Latin-1) | Register Unicode TrueType fonts (e.g., Google Noto Sans TTF) with ReportLab's `TTFont`. |
| **ZIP Generation** | Generated dynamically to temporary file | Asynchronous ZIP generation saved to object storage with download notification/webhook. |

---

## 10. Verification & Test Summary

All requirements are covered by **32 automated tests** across 6 test modules:

- `test_create_job.py`: Job creation, 202 response, HATEOAS links, DB count persistence, idempotency key deduplication.
- `test_validation.py`: Level 1 request rejections (422) for empty arrays, max limit, blank event; Level 2 row failures for empty names, invalid email syntax, duplicate emails, non-dict payloads, and pure `validate_recipient()` unit tests.
- `test_generation.py`: Binary `%PDF` validation, `pypdf` textual inspection of recipient, title, and event names, serial uniqueness, and long name formatting.
- `test_job_status.py`: State machine transitions (`completed`, `completed_with_errors`, `failed`), progress calculation, pagination (`limit`/`offset`), status filters, partial progress seeding, startup crash recovery, optional field omissions, and 404 handling for ZIP and retry endpoints.
- `test_failure_isolation.py`: Monkeypatched generator exception isolation, `GENERATION_ERROR` logging, and `retry-failed` recovery flow.
- `test_retrieval.py`: Single certificate download (200, Content-Disposition), 409 conflict checks on non-ready (pending and failed) states, 404s, ZIP archive validation, and ZIP 409 handling.
