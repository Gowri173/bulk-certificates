# Bulk Certificate Generator: Backend Design + Master Prompt

This file has two parts:

1. **Design document** (what we build and why). Read this first so you can explain every decision in the interview.
2. **Master prompt** (Part 2). Paste it into your AI coding tool (Claude Code, Cursor, etc.) to generate the project.

---

# PART 1: DESIGN DOCUMENT

## 1. Goals and non-goals

**Goals**
- Accept one request containing many recipients (bulk), not one request per certificate.
- Validate each recipient individually; bad rows must not block good rows.
- Generate a PDF certificate per valid recipient from one predefined template.
- Track per-certificate and per-job status; expose progress.
- Let the client download one certificate or all certificates of a job (ZIP).
- Be explainable, testable, and easy to modify in an interview.

**Non-goals**: template editor, multiple designs, auth/multi-tenancy, email delivery, cloud storage. (Mentioned as extensions in section 14.)

## 2. Tech stack and why

| Concern | Choice | Reason |
|---|---|---|
| Framework | **FastAPI** | Pydantic validation, auto OpenAPI docs at `/docs`, simple dependency injection, easy testing with `TestClient`. |
| ORM | **SQLAlchemy 2.0** (sync) + **Alembic** (optional) | Mature, works with SQLite and PostgreSQL. |
| Database | **SQLite by default**, PostgreSQL via `DATABASE_URL` | Zero-setup for reviewers; relational as required; same code works on Postgres. |
| PDF | **ReportLab** | Pure Python, no system packages (unlike WeasyPrint/wkhtmltopdf), fully programmable layout, deterministic output. |
| Background work | In-process **ThreadPoolExecutor** worker + DB as source of truth | No Redis/Celery to install; see section 5 for the tradeoff and upgrade path. |
| Tests | **pytest** + FastAPI `TestClient` + temp SQLite + temp storage dir | Fast, isolated. |
| Config | `pydantic-settings` reading env vars / `.env` | Typed config. |

## 3. Processing model: synchronous vs background (the documented decision)

**Chosen: asynchronous background processing, job-based API.**

- `POST` validates the request, persists the job and one row per recipient, enqueues the job, and returns **`202 Accepted`** with a `job_id` immediately.
- A background worker generates PDFs and updates each row as it goes.
- The client polls `GET /jobs/{id}` for progress.

**Why not synchronous?** Generating thousands of PDFs takes seconds to minutes. A synchronous request would hit HTTP/proxy timeouts, tie up a server worker, and lose all progress if the connection dropped.

**Why not Celery/Redis?** It is the production answer, but adds infrastructure that makes the project harder to run and explain for an assessment. Instead the design isolates the queue behind one small function (`enqueue(job_id)`), so swapping to Celery/RQ/Arq changes one file.

**Reliability measures (important for the interview)**
- The **database is the source of truth**, not memory. Every certificate row has a status.
- Each certificate is processed in its **own try/except and own commit**, so one failure never aborts the job and progress survives a crash.
- **Startup recovery**: on app start, jobs in `pending` or `processing` are re-enqueued; only certificate rows still `pending` are processed (rows marked `processing` by a dead worker are reset to `pending`). So work is **idempotent and resumable**.
- A **test/inline mode** (`PROCESSING_MODE=inline`) runs the job synchronously inside `enqueue()` so tests are deterministic with no sleeping or polling.

**Known limitation to state honestly:** with the in-process worker, running multiple uvicorn workers means each has its own pool. Safe because rows are claimed with an atomic `UPDATE ... WHERE status='pending'`, but a real deployment should use a dedicated queue (Celery/RQ).

## 4. Architecture

```
Client
  |  POST /api/v1/certificate-jobs  (bulk)
  v
[ API layer: FastAPI routers ]  -> schemas (Pydantic), status codes, pagination
  v
[ Service layer: job_service ]  -> validation per recipient, create job + rows, summary
  v                                   |
[ DB (SQLAlchemy) ] <-----------------+
  ^
  |   enqueue(job_id)
[ Worker: processor ] -> for each pending row:
                           claim -> generate PDF -> store file -> mark success/failed -> update counters
                               |
                    [ CertificateGenerator (ReportLab) ]   [ Storage (local disk) ]
```

Layer rules: routers contain no business logic; services contain no HTTP details; generator knows nothing about the DB; storage is behind an interface.

## 5. Project structure

```
bulk-certificates/
├── app/
│   ├── main.py                 # app factory, lifespan (create tables, recovery), routers
│   ├── config.py               # Settings (DATABASE_URL, STORAGE_DIR, MAX_RECIPIENTS, PROCESSING_MODE, WORKERS)
│   ├── database.py             # engine, SessionLocal, Base, get_db dependency
│   ├── models.py               # Job, Certificate
│   ├── schemas.py              # request/response Pydantic models
│   ├── enums.py                # JobStatus, CertificateStatus, ErrorCode
│   ├── api/
│   │   ├── jobs.py             # job endpoints
│   │   └── certificates.py     # download endpoints
│   ├── services/
│   │   ├── job_service.py      # create_job, get_job, list_certificates, retry_failed
│   │   ├── validation.py       # validate_recipient() -> (clean_data | error)
│   │   └── zip_service.py      # stream ZIP of successful certificates
│   ├── worker/
│   │   ├── queue.py            # enqueue(), executor, startup recovery
│   │   └── processor.py        # process_job(job_id)
│   ├── generator/
│   │   ├── base.py             # CertificateGenerator protocol
│   │   └── pdf.py              # ReportLab implementation of the single template
│   └── storage/
│       ├── base.py             # Storage protocol: save(path, bytes), open(path), exists()
│       └── local.py            # local-disk implementation
├── tests/
│   ├── conftest.py             # temp DB, temp storage, inline mode, client fixture
│   ├── test_create_job.py
│   ├── test_validation.py
│   ├── test_generation.py
│   ├── test_job_status.py
│   ├── test_failure_isolation.py
│   └── test_retrieval.py
├── requirements.txt
├── .env.example
├── README.md
└── pytest.ini
```

## 6. Data model

### `jobs`
| Column | Type | Notes |
|---|---|---|
| id | UUID (string PK) | public identifier |
| status | enum | `pending`, `processing`, `completed`, `completed_with_errors`, `failed` |
| event_name | str(200) | required, printed on certificate (e.g. "Python Bootcamp 2026") |
| title | str(100) | default "Certificate of Completion" |
| issuer_name | str(150), nullable | e.g. organization name |
| issued_on | date | default today |
| total_count | int | recipients submitted |
| success_count | int | |
| failed_count | int | |
| idempotency_key | str, nullable, unique | optional safe-retry support |
| created_at, started_at, completed_at | datetime (UTC) | `started_at` / `completed_at` nullable |

### `certificates` (one row per submitted recipient, valid or not)
| Column | Type | Notes |
|---|---|---|
| id | UUID PK | |
| job_id | FK -> jobs.id, indexed | |
| row_index | int | position in the original request (so clients can map errors back) |
| recipient_name | str, nullable | raw value may be invalid, so nullable |
| recipient_email | str, nullable | |
| status | enum | `pending`, `processing`, `success`, `failed` |
| error_code | str, nullable | `VALIDATION_ERROR`, `DUPLICATE_RECIPIENT`, `GENERATION_ERROR` |
| error_message | text, nullable | human-readable reason |
| serial_number | str, unique, nullable | e.g. `CERT-2026-8F3A9C21`; printed on certificate |
| file_path | str, nullable | relative storage path |
| created_at, generated_at | datetime | |

**Indexes / constraints**: index on `(job_id, status)`; unique `(job_id, recipient_email)` is *not* enforced in the DB (invalid rows may lack an email), so duplicate detection is done in the service layer, and the duplicate row is stored as `failed`.

**Why store invalid recipients as rows?** The job status must "identify successful and failed generations". Storing rejected rows with `row_index` and a reason gives one uniform list of results and lets the client fix and resubmit exactly those rows.

## 7. Validation strategy (two levels)

**Level 1: request-level (whole request rejected, `422`)**
- Body is not valid JSON or `recipients` missing / not a list.
- `recipients` empty, or more than `MAX_RECIPIENTS` (default 5000) -> `422` (or `413` for too large; pick `422` and document).
- `event_name` missing/blank or too long; `issued_on` not a valid date.

**Level 2: recipient-level (row marked `failed`, job still accepted)**
`recipients` is declared as a list of loosely typed objects so one bad item does not make Pydantic reject the entire body. Each item is validated individually in `validation.py`:
- `name`: required, string, trimmed, 1-100 chars, no control characters.
- `email`: required, valid format (use `email-validator`), lowercased.
- Duplicate email within the same request -> second and later occurrences fail with `DUPLICATE_RECIPIENT`.
- Optional per-recipient field: `achievement` (str <= 200), printed under the name if provided.

Response includes counts so the client sees rejects instantly (`rejected_count`), and full detail is available through the job's certificate list.

## 8. API specification

Base path: `/api/v1`

### 8.1 `POST /certificate-jobs`: create a bulk job
Optional header: `Idempotency-Key: <string>`: same key returns the existing job instead of creating a duplicate.

Request:
```json
{
  "event_name": "Python Bootcamp 2026",
  "title": "Certificate of Completion",
  "issuer_name": "Acme Academy",
  "issued_on": "2026-10-07",
  "recipients": [
    {"name": "Asha Rao", "email": "asha@example.com", "achievement": "Distinction"},
    {"name": "Ravi Kumar", "email": "ravi@example.com"},
    {"name": "", "email": "bad-email"}
  ]
}
```
Response `202 Accepted`:
```json
{
  "job_id": "5f1c...",
  "status": "pending",
  "total_count": 3,
  "accepted_count": 2,
  "rejected_count": 1,
  "links": {
    "self": "/api/v1/certificate-jobs/5f1c...",
    "certificates": "/api/v1/certificate-jobs/5f1c.../certificates",
    "download_all": "/api/v1/certificate-jobs/5f1c.../download"
  }
}
```
Errors: `422` request-level validation (FastAPI default shape or a custom `{"detail": [...]}`).

### 8.2 `GET /certificate-jobs/{job_id}`: status and progress
```json
{
  "job_id": "5f1c...",
  "status": "completed_with_errors",
  "event_name": "Python Bootcamp 2026",
  "total_count": 3,
  "success_count": 2,
  "failed_count": 1,
  "pending_count": 0,
  "progress_percent": 100.0,
  "created_at": "...", "started_at": "...", "completed_at": "..."
}
```
`progress_percent = (success + failed) / total * 100`. `404` if unknown.

### 8.3 `GET /certificate-jobs/{job_id}/certificates`
Query: `status=success|failed|pending|processing`, `limit` (default 50, max 500), `offset`.
```json
{
  "total": 3, "limit": 50, "offset": 0,
  "items": [
    {"id": "c1...", "row_index": 0, "recipient_name": "Asha Rao", "recipient_email": "asha@example.com",
     "status": "success", "serial_number": "CERT-2026-8F3A9C21",
     "download_url": "/api/v1/certificates/c1.../download", "error_code": null, "error_message": null},
    {"id": "c3...", "row_index": 2, "recipient_name": "", "recipient_email": "bad-email",
     "status": "failed", "serial_number": null, "download_url": null,
     "error_code": "VALIDATION_ERROR", "error_message": "name: must not be empty; email: invalid format"}
  ]
}
```

### 8.4 `GET /certificates/{certificate_id}/download`
Returns the PDF (`application/pdf`, `Content-Disposition: attachment; filename="<serial>.pdf"`).
`404` unknown id; `409` if the certificate is not `success` yet (pending/failed) with a clear message.

### 8.5 `GET /certificate-jobs/{job_id}/download`
Streams a ZIP of all **successful** certificates (`certificates-<job_id>.zip`). Files named `<serial>_<safe-name>.pdf`. `409` if job has zero successes or no certificates exist yet. Safe against path traversal (names sanitized; paths come from DB, never from the client).

### 8.6 `POST /certificate-jobs/{job_id}/retry-failed` (small optional improvement)
Re-queues only rows with `error_code = GENERATION_ERROR` (validation failures cannot succeed without new data). Returns `202`. Skip if time is short.

### 8.7 `GET /health`
`{"status": "ok"}`.

## 9. Job status state machine

```
pending ──worker picks up──> processing ──all rows done──> completed              (failed_count == 0)
                                  │                    └─> completed_with_errors  (some success, some failed)
                                  └──────────────────────> failed                 (zero successes)
```
- A job with only invalid recipients: no rows to process -> becomes `failed` immediately, still `202` at submit (documented).
- Counters are recomputed from rows (`SELECT status, COUNT(*) ... GROUP BY status`) at the end and every N=25 certificates, so they cannot drift.

## 10. Worker algorithm (`process_job`)

```
process_job(job_id):
  set job.status = processing, started_at = now  (only if pending/processing)
  reset rows stuck in 'processing' -> 'pending'     # crash recovery
  loop: fetch up to BATCH_SIZE (e.g. 50) rows where job_id and status='pending' order by row_index
     for each row:
        claim: UPDATE certificates SET status='processing' WHERE id=:id AND status='pending'
               if rowcount == 0: continue                      # another worker took it
        try:
            serial = generate unique serial
            pdf_bytes = generator.generate(CertificateData(...))
            path = storage.save(f"{job_id}/{row.id}.pdf", pdf_bytes)
            row.status='success', row.file_path=path, row.serial_number=serial, generated_at=now
        except Exception as e:
            row.status='failed', error_code='GENERATION_ERROR', error_message=str(e)[:500]
            log.exception(...)
        commit                                                   # per-certificate commit
     every 25 rows: refresh job counters
  finalize: recompute counters, set final status + completed_at
```
Key point: the `try/except` wraps **one certificate**, and the **outer loop never breaks** on a single failure. If the whole worker crashes, DB state plus startup recovery resumes it.

## 11. Certificate template (single, predefined)

ReportLab, A4 landscape, drawn in code (no external assets so it works anywhere):
- Double border frame, accent colour.
- Title (from job `title`), "This is to certify that", **recipient name** (centered, auto-shrinks font to fit width; long names are truncated with ellipsis only as a last resort), optional achievement line, "has successfully completed", **event name**, issue date, issuer name, **serial number** bottom corner.
- Font: built-in Helvetica/Times. Limitation to document: non-Latin scripts need a bundled TTF (e.g., Noto Sans) registered with ReportLab. Mention as extension; handle by catching the encoding error as a `GENERATION_ERROR` if unsupported glyphs would render wrongly.
- Generator is a pure function: `CertificateData -> bytes`. Easy to unit test (assert it starts with `%PDF` and, using `pypdf`, that the name text appears).

## 12. Error handling and HTTP status codes

| Situation | Code |
|---|---|
| Job accepted | 202 |
| Request-level validation failure | 422 |
| Unknown job/certificate | 404 |
| Certificate/job not downloadable yet or failed | 409 with message |
| Duplicate Idempotency-Key | 200 or 202 returning the original job |
| Unexpected server error | 500 with generic body, details only in logs |

Error body convention: `{"detail": "..."}`. Never leak stack traces or filesystem paths.

## 13. Testing plan (maps 1:1 to the assignment's minimum list)

All tests use a temp SQLite DB, a temp storage dir, and `PROCESSING_MODE=inline`.

| Test file | Cases |
|---|---|
| `test_create_job.py` | valid request returns 202 + job_id; counts correct; rows created in DB; idempotency key returns same job; response links present |
| `test_validation.py` | empty recipients list -> 422; > MAX -> 422; missing event_name -> 422; bad email/blank name -> row `failed` with `VALIDATION_ERROR`, others still succeed; duplicate email -> `DUPLICATE_RECIPIENT`; whitespace trimmed / email lowercased |
| `test_generation.py` | a PDF file exists on disk per success; bytes start with `%PDF`; PDF text contains name and event (via `pypdf`); serial numbers unique; template handles very long names |
| `test_job_status.py` | status transitions to `completed` / `completed_with_errors` / `failed`; counts and `progress_percent`; `404` unknown job; pagination and `status` filter on certificate list; partial progress state (call processor with a limited batch or a pre-seeded DB) |
| `test_failure_isolation.py` | monkeypatch generator to raise for one recipient; that row is `failed` with `GENERATION_ERROR`; all others `success`; job ends `completed_with_errors`; retry-failed re-processes only that row (if implemented) |
| `test_retrieval.py` | download single PDF (200, `application/pdf`); 409 for failed/pending cert; 404 unknown; ZIP contains only successful certs and correct count; ZIP for job with no successes -> 409 |

## 14. Scaling and change-requirement answers (interview prep)

- **"10x more recipients?"** Move to Celery/RQ with Redis, one task per chunk of N certificates, Postgres instead of SQLite, S3 for storage, ZIP built asynchronously and stored.
- **"Add a second template?"** Add `template_id` to jobs, a registry `{template_id: Generator}`, validate id at request level.
- **"Email the certificate to each recipient?"** Add `email_status` column + separate task after generation (outbox pattern), so email failures don't affect generation status.
- **"Add API authentication / multi-tenant?"** Add `owner_id` to jobs, API-key or JWT dependency, filter every query by owner.
- **"Webhook when job finishes?"** Optional `callback_url` on job; send in finalize step with retries.
- **"Progress in real time?"** Server-Sent Events endpoint reading the same counters.
- **"Why not one big transaction?"** Per-row commits give crash-safe progress and isolation, at small throughput cost (batching can be added).
- **"Why store failed rows?"** Uniform results, easy client remediation, auditability.
- **"Sync vs async?"** Section 3.

## 15. Security and robustness notes
- Filenames come from DB ids/serials, not user input; ZIP entry names sanitized (`[A-Za-z0-9._-]`).
- Max body size and `MAX_RECIPIENTS` guard against abuse.
- Strings validated for length/control characters before rendering.
- Storage path joined and checked to stay inside `STORAGE_DIR`.
- No secrets in repo; `.env.example` provided.

## 16. Configuration (`.env.example`)
```
DATABASE_URL=sqlite:///./certificates.db
STORAGE_DIR=./storage
MAX_RECIPIENTS=5000
PROCESSING_MODE=background     # background | inline (tests)
WORKER_THREADS=2
BATCH_SIZE=50
LOG_LEVEL=INFO
```

## 17. Definition of done
- All endpoints in section 8 work and appear in `/docs`.
- `pytest` passes from a clean clone.
- README lets a stranger run the app and submit a request with `curl` in under 5 minutes.
- You can explain: processing model, state machine, failure isolation, crash recovery, and validation levels without reading the code.

---

# PART 2: MASTER PROMPT (paste into your AI coding tool)

````text
You are a senior Python backend engineer. Build a complete, runnable project called
"bulk-certificates": a backend API that accepts bulk certificate generation requests,
generates PDF certificates from ONE predefined template, tracks status, and lets clients
retrieve certificates. Follow the specification below exactly. Prefer simple, readable,
well-commented code over clever code: the author must be able to explain and modify every
part in a technical interview. Do not add features beyond this spec except where marked optional.

## TECH STACK (fixed)
- Python 3.11+, FastAPI, Uvicorn
- SQLAlchemy 2.0 (sync ORM, typed `Mapped[]` style). SQLite by default, PostgreSQL-compatible via DATABASE_URL
- Pydantic v2 + pydantic-settings, email-validator
- ReportLab for PDF generation
- pytest, httpx (for TestClient), pypdf (tests only, to read PDF text)
- No Redis/Celery. Background work uses an in-process ThreadPoolExecutor.

## PROJECT STRUCTURE (create exactly this)
app/main.py, app/config.py, app/database.py, app/models.py, app/schemas.py, app/enums.py,
app/api/jobs.py, app/api/certificates.py,
app/services/job_service.py, app/services/validation.py, app/services/zip_service.py,
app/worker/queue.py, app/worker/processor.py,
app/generator/base.py, app/generator/pdf.py,
app/storage/base.py, app/storage/local.py,
tests/conftest.py, tests/test_create_job.py, tests/test_validation.py, tests/test_generation.py,
tests/test_job_status.py, tests/test_failure_isolation.py, tests/test_retrieval.py,
requirements.txt, .env.example, pytest.ini, README.md, .gitignore

## CONFIG (app/config.py, env-driven)
DATABASE_URL (default sqlite:///./certificates.db), STORAGE_DIR (./storage), MAX_RECIPIENTS (5000),
PROCESSING_MODE ("background" | "inline"), WORKER_THREADS (2), BATCH_SIZE (50), LOG_LEVEL (INFO).
"inline" mode runs process_job synchronously inside enqueue() and is used by tests.

## DATA MODEL
Enums: JobStatus = pending | processing | completed | completed_with_errors | failed.
CertificateStatus = pending | processing | success | failed.
ErrorCode = VALIDATION_ERROR | DUPLICATE_RECIPIENT | GENERATION_ERROR.

Table jobs: id (uuid4 string PK), status, event_name (str 200), title (str 100, default
"Certificate of Completion"), issuer_name (nullable str 150), issued_on (date, default today),
total_count, success_count, failed_count (ints), idempotency_key (nullable, unique),
created_at, started_at (nullable), completed_at (nullable); all datetimes UTC.

Table certificates: id (uuid4 string PK), job_id (FK jobs.id, indexed), row_index (int, position in
the original request), recipient_name (nullable), recipient_email (nullable), achievement (nullable),
status, error_code (nullable), error_message (nullable text), serial_number (nullable, unique),
file_path (nullable, relative to STORAGE_DIR), created_at, generated_at (nullable).
Composite index on (job_id, status). Create tables on startup (Alembic not required).

## VALIDATION (two levels)
Level 1, request-level, reject the whole request with 422:
- body not valid; `recipients` missing, not a list, empty, or longer than MAX_RECIPIENTS
- `event_name` missing/blank/too long; `issued_on` not a valid date; `title`/`issuer_name` too long
The schema must declare `recipients` as a list of loosely typed dicts so that ONE malformed recipient
does NOT make Pydantic reject the entire request.

Level 2, per recipient (app/services/validation.py), never rejects the job; the row is stored with
status=failed + error_code + readable error_message and the job is still accepted:
- name: required string, trimmed, 1..100 chars, no control characters
- email: required, valid per email-validator, lowercased
- achievement: optional string <= 200 chars
- duplicate email within the same request: first occurrence kept, later ones fail with DUPLICATE_RECIPIENT
- non-dict items or wrong types -> VALIDATION_ERROR with a clear message
Collect ALL problems for a row into one message (e.g. "name: must not be empty; email: invalid format").
Validation function must be pure and unit-testable: validate_recipient(raw: Any) -> ValidationResult.

## API (prefix /api/v1)
1. POST /certificate-jobs -> 202. Body: event_name, title?, issuer_name?, issued_on?, recipients[].
   Optional header Idempotency-Key: if a job with that key exists, return it (do not create another).
   Create job + one certificates row per submitted recipient (valid -> pending, invalid -> failed) in ONE
   transaction, then call enqueue(job_id) AFTER commit. Response: job_id, status, total_count, accepted_count,
   rejected_count, links {self, certificates, download_all}.
   If there are zero valid recipients, set job status=failed and completed_at immediately (still 202).
2. GET /certificate-jobs/{job_id} -> job status: job_id, status, event_name, total_count, success_count,
   failed_count, pending_count (pending + processing), progress_percent = (success+failed)/total*100 rounded
   to 1 decimal, created_at, started_at, completed_at. 404 if not found.
3. GET /certificate-jobs/{job_id}/certificates?status=&limit=50&offset=0 (limit max 500) ->
   {total, limit, offset, items[]}; item: id, row_index, recipient_name, recipient_email, status, serial_number,
   download_url (only when success), error_code, error_message. Ordered by row_index. 404 for unknown job.
4. GET /certificates/{certificate_id}/download -> PDF (application/pdf, attachment filename "<serial>.pdf").
   404 unknown; 409 with explanatory message if status is not success.
5. GET /certificate-jobs/{job_id}/download -> ZIP of all SUCCESSFUL certificates, streamed or built with
   tempfile (never hold thousands of PDFs in memory). Entry names "<serial>_<sanitized-name>.pdf" where
   sanitized = only [A-Za-z0-9._-]. 404 unknown job; 409 if no successful certificates yet.
6. POST /certificate-jobs/{job_id}/retry-failed -> 202. Re-queue only rows with error_code=GENERATION_ERROR
   (reset to pending, clear error, job back to pending, completed_at null) then enqueue. (Optional but implement it.)
7. GET /health -> {"status":"ok"}.
Use proper response_model schemas, tags, and docstrings so /docs is clear. Error body: {"detail": "..."}.
Never leak stack traces or filesystem paths in responses.

## WORKER (app/worker/)
queue.py: a module-level ThreadPoolExecutor(WORKER_THREADS); enqueue(job_id) submits process_job, or runs it
inline when PROCESSING_MODE=inline. recover_incomplete_jobs() runs at app startup (FastAPI lifespan): find jobs
in pending/processing and enqueue them. Executor shut down cleanly on app shutdown.

processor.py process_job(job_id) must use its OWN DB session (never the request session) and:
1. Load job; set status=processing and started_at if not already set.
2. Reset any certificate rows stuck in `processing` back to `pending` (crash recovery).
3. Loop: fetch up to BATCH_SIZE rows with status=pending ordered by row_index, until none remain.
4. For each row: CLAIM it atomically with `UPDATE certificates SET status='processing' WHERE id=:id AND
   status='pending'`; if rowcount==0 skip it. Then inside a try/except around ONLY this one certificate:
   generate a unique serial (format CERT-<year>-<8 hex uppercase>, retry on collision), call the generator,
   save via storage at "<job_id>/<certificate_id>.pdf", mark success with file_path, serial_number, generated_at.
   On ANY exception: mark failed, error_code=GENERATION_ERROR, error_message=str(e)[:500], log with
   logger.exception, and CONTINUE. Commit after EACH certificate.
5. Every 25 certificates and at the end, recompute success_count/failed_count from the rows (GROUP BY status),
   do not increment counters in memory.
6. Finalize: if no successes -> failed; elif no failures -> completed; else completed_with_errors;
   set completed_at. Wrap the whole function in a top-level try/except that logs and, if something truly
   unexpected happens, leaves rows resumable (do not mark healthy rows failed).

## GENERATOR (app/generator/)
base.py: dataclass CertificateData(recipient_name, achievement, event_name, title, issuer_name, issued_on,
serial_number) and a CertificateGenerator Protocol with `generate(data) -> bytes`.
pdf.py: ReportLab, A4 landscape, ONE hard-coded template drawn in code (no external assets): double border
frame with an accent colour, centered title, "This is to certify that", recipient name in a large font that
auto-shrinks to fit the page width (min font size, then truncate with "..."), optional achievement line,
"has successfully completed", event name, issue date, issuer name (if any), serial number bottom-left.
Return bytes via BytesIO. Use built-in fonts; add a comment documenting that non-Latin scripts need a bundled
TTF. A module-level `get_generator()` returns the PDF generator so tests can monkeypatch it.
The processor must obtain the generator through get_generator() at call time (not import-time binding).

## STORAGE (app/storage/)
base.py: Storage Protocol: save(relative_path, data: bytes) -> str, open_path(relative_path) -> Path, exists(path).
local.py: saves under STORAGE_DIR, creates directories, and verifies the resolved path stays inside STORAGE_DIR
(raise on traversal). Paths in DB are relative.

## SERVICES
job_service.py holds create_job (with idempotency), get_job_or_404, job_summary (counts via GROUP BY),
list_certificates (filter + pagination), retry_failed. API routers stay thin: parse -> call service -> map to schema.

## LOGGING
Standard `logging`, configured from LOG_LEVEL. Log job lifecycle events (created, started, finished with counts)
and every per-certificate failure with job_id and certificate_id.

## TESTS (pytest; every test isolated)
conftest.py: fixtures that set env/settings to a temp SQLite file and tmp_path storage dir with
PROCESSING_MODE=inline, create tables fresh per test, provide a TestClient, and a helper `make_payload(n)`.
Write these tests (clear names, one behaviour each):
- test_create_job: valid request -> 202 with job_id + links + correct counts; DB has N certificate rows;
  same Idempotency-Key twice -> same job_id and no duplicate rows.
- test_validation: empty list 422; > MAX_RECIPIENTS 422 (patch the setting low); missing event_name 422;
  blank name / bad email -> row failed VALIDATION_ERROR while other rows succeed; duplicate email ->
  DUPLICATE_RECIPIENT; name trimmed and email lowercased; non-dict recipient item handled; unit tests of
  validate_recipient directly.
- test_generation: success rows have files on disk; bytes start with b"%PDF"; pypdf text contains recipient
  name and event name; serial numbers unique; a very long name still generates.
- test_job_status: final status completed / completed_with_errors / failed in the three scenarios; counts and
  progress_percent correct; 404 for unknown job; certificate list pagination and ?status= filter; a progress
  test that seeds a job with some rows pending and some success and checks progress_percent < 100 and
  pending_count.
- test_failure_isolation: monkeypatch get_generator so it raises for one specific name; that row is failed with
  GENERATION_ERROR and message; all others success; job completed_with_errors; retry-failed after fixing the
  patch makes that row success and job completed.
- test_retrieval: single download 200 + application/pdf + body starts with %PDF; 409 for failed and pending rows;
  404 unknown id; ZIP download contains exactly the successful certificates and valid PDFs; ZIP for a job with no
  successes -> 409; download_url present only for successful rows.

## README.md (required sections)
Overview; Tech stack; Setup (venv, pip install -r requirements.txt, cp .env.example .env); Run (uvicorn
app.main:app --reload; docs at /docs); Run tests (pytest); Submit a request (full curl example with 3 recipients
including one invalid); Check status (curl); List certificates (curl, with ?status=failed); Download one
certificate and download ZIP (curl -o); Retry failed; Design decisions (sync vs background reasoning, DB as source of
truth, per-certificate commit and failure isolation, two-level validation, why failed rows are stored, idempotency,
crash recovery, storage abstraction); Known limitations (in-process worker, local disk, Latin-only fonts, SQLite
write concurrency) and how to scale (Celery/RQ + Postgres + S3).

## QUALITY BAR
- Type hints everywhere, small functions, docstrings explaining WHY.
- No dead code, no TODO placeholders, no unused dependencies. requirements.txt pinned with compatible versions.
- Everything must run from a clean clone: `pip install -r requirements.txt && pytest` passes, and
  `uvicorn app.main:app` starts and serves /docs.
- After generating all files, run the tests, fix any failures, then print a short summary of how each requirement
  in the assignment maps to the code (file + function).
````

---

## Quick interview cheat sheet

1. **Why async?** Long-running bulk work; avoid timeouts; 202 + polling.
2. **How does one failure not stop the job?** The `try/except` wraps a single certificate, with a commit per row, and the loop continues.
3. **What if the server crashes mid-job?** DB holds row states; startup recovery resets `processing` -> `pending` and resumes.
4. **How do you avoid double-processing?** Atomic `UPDATE ... WHERE status='pending'` claim.
5. **Why store invalid rows?** One uniform result list and an easy fix-and-resubmit workflow.
6. **Biggest limitation?** In-process worker; the fix is Celery/RQ + Postgres + S3, with the change isolated to `worker/queue.py` and `storage/`.
