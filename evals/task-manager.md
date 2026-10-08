# Task Manager — first test input (Phase 0.4)

Build a REST API for managing tasks with SQLite persistence.

## Requirements

1. `GET /health` returns 200 `{"status": "ok"}`.
2. `POST /tasks` with `{title, description?, done?, due?}` returns 201 with `id`.
   Missing/empty `title` returns 422.
3. `GET /tasks` returns list containing created tasks.
4. `GET /tasks/{id}` returns task, unknown id returns 404.
5. `PUT /tasks/{id}` updates fields, unknown id returns 404.
6. `DELETE /tasks/{id}` returns 200, subsequent GET returns 404, unknown id returns 404.

## Acceptance criteria

* All 6 behaviours above covered by tests (success + validation + error).
* App boots in sandbox within 30s.
* `ruff` clean of syntax errors, no string-concatenated SQL.
