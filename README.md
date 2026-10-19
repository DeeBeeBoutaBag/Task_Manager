# Task Manager API

A small FastAPI service for creating, listing, updating, and deleting tasks.
Tasks live in a Python dictionary. The implementation includes the required CRUD
endpoints, consistent JSON errors, tests, and all three bonuses: dependency
injection, pagination, and background file logging.

## Run locally

Use **Python 3.11 or newer**. From this folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload
```

On Windows, use `py -m venv .venv` and `.venv\Scripts\activate` in Command Prompt instead of the first two commands.

Open [the interactive API docs](http://127.0.0.1:8000/docs) to try each endpoint.
The server uses one process; restarting it or triggering a development reload
clears the tasks.

## Run the tests

With the virtual environment activated, run:

```bash
python -m pytest -q
```

The manager covers creation, missing tasks, completing a task, filters, partial
updates, invalid input, deletion, pagination, background logging, and consistent
error responses. Each test gets fresh storage through a dependency override and
a temporary log path, so it does not write to your application log.

Verified with Python 3.13.3: **45 tests passed**.

## Endpoints

| Method | Path | Success | Purpose |
| --- | --- | --- | --- |
| POST | `/tasks` | 201 | Create a task; return it and a `Location` header |
| GET | `/tasks` | 200 | List a page of tasks; optionally filter by `completed` |
| GET | `/tasks/{task_id}` | 200 | Fetch one task |
| PATCH | `/tasks/{task_id}` | 200 | Update only supplied fields |
| DELETE | `/tasks/{task_id}` | 204 | Delete a task; return an empty body |

Creation requires `title`. `description` defaults to `null`, and `completed`
defaults to `false`. The server generates a UUID and a timezone-aware UTC
`created_at` value.

Create a task:

```bash
curl -i -X POST http://127.0.0.1:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{"title": "Review the assessment", "description": "Check the edge cases"}'
```

Copy the returned ID into the URL to complete it:

```bash
curl -X PATCH http://127.0.0.1:8000/tasks/PASTE_ID_HERE \
  -H 'Content-Type: application/json' \
  -d '{"completed": true}'
```

## Pagination (Bonus B)

`GET /tasks` accepts `limit` (default 10, from 1 to 100) and `offset` (default 0,
minimum 0). Offset means how many matching tasks to skip. For example:

```bash
curl 'http://127.0.0.1:8000/tasks?completed=false&limit=5&offset=0'
```

The response is always an object containing `total`, `items`, `limit`, `offset`,
`next_offset`, and `previous_offset`. With no matching tasks, it looks like:

```json
{
  "total": 0,
  "items": [],
  "limit": 5,
  "offset": 0,
  "next_offset": null,
  "previous_offset": null
}
```

The completion filter runs before counting or paging, so `total` is the number
of matching tasks across all pages. Items stay in insertion order. Use
`next_offset` or `previous_offset` in the next request, keeping the same `limit`
and filter. `null` means there is no page in that direction. An offset beyond
the results returns 200 with empty `items`; its `previous_offset` still steps
back by `limit`, stopping at zero.

## Background logging (Bonus C)

Successful creates and deletes schedule a small function through FastAPI's
`BackgroundTasks`. After the response is sent, it appends the current UTC time,
the action (`created` or `deleted`), and the task UUID to `task_events.log` next
to `main.py`. The file is created on the first successful write. Failed requests
do not schedule an event.

The function uses a lock so background writes in this process do not overlap.
If a file write fails, the server logs the error; the successful task operation is not undone. This is best-effort logging in the running process, with no durable queue or retries. The log file survives restarts; the task dictionary does not. No extra dependencies are needed.

## Validation and errors

- Titles are trimmed, and empty or whitespace-only titles are rejected.
- On PATCH, omitted fields stay unchanged. `description: null` clears the
  description; `title: null` and `completed: null` are rejected.
- An empty PATCH body `{}` returns the existing task unchanged.
- Extra body fields are rejected, including client-supplied `id` and `created_at`.
- Invalid bodies, UUIDs, completion filters, and pagination values return 422.
  A valid UUID that does not identify a stored task returns 404.
- Unknown routes have a custom 404 message. Unsupported methods return 405.

Errors use a string under `detail`, for example:

```json
{"detail": "Task not found."}
```

Validation errors include the field location. Unexpected failures return a
generic 500 response while the exception is logged on the server.

## My Design choices

`main.py` contains the input/output models, storage dependency, error handlers,
and five routes. Keeping this small manager in one module makes the request flow easy to follow. `test_main.py` contains the tests.

Separate create and update models distinguish required creation fields from
optional update fields. PATCH uses `model_dump(exclude_unset=True)` so model defaults do not overwrite existing values.

The `get_task_store` dependency keeps routes from accessing the global dictionary
directly and lets tests inject an empty dictionary. It also provides a starting
point for a database implementation. A real database would still require changes
to the dictionary operations and transaction handling.

The async route handlers perform only brief in-memory operations. File writes
run in the synchronous background function, which FastAPI runs in a thread pool.
There are no `await` points inside a task update. Storage is local to one process and is not shared between workers. if this was a larger service, the next steps would be persistent storage, durable event delivery, and conflict handling for competing updates.

AI tools assisted with the validation and test review.
