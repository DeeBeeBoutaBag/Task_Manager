from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import main
from main import app, get_task_store


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Each test gets its own dictionary, so test order doesn't affect the results.
    task_store = {}
    app.dependency_overrides[get_task_store] = lambda: task_store
    # Background jobs still write real files, but only inside pytest's temp folder.
    monkeypatch.setattr(main, "LOG_FILE", tmp_path / "task_events.log")
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def task(client):
    response = client.post(
        "/tasks", json={"title": "Prepare interview", "description": "Review the API"}
    )
    assert response.status_code == 201
    return response.json()


def assert_error(response, status_code):
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"detail"}
    assert isinstance(body["detail"], str)
    assert body["detail"]


def test_create_task(client):
    before_creation = datetime.now(timezone.utc)
    response = client.post("/tasks", json={"title": "  Write tests  "})

    assert response.status_code == 201
    task = response.json()
    assert str(UUID(task["id"])) == task["id"]
    assert task["title"] == "Write tests"
    assert task["description"] is None
    assert task["completed"] is False
    assert response.headers["location"] == f"/tasks/{task['id']}"

    created_at = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
    assert created_at.utcoffset() == timedelta(0)
    assert before_creation <= created_at <= datetime.now(timezone.utc)
    assert client.get(response.headers["location"]).json() == task


def test_create_task_with_optional_fields(client):
    response = client.post(
        "/tasks",
        json={"title": "Read requirements", "description": "Done today", "completed": True},
    )

    assert response.status_code == 201
    assert response.json()["description"] == "Done today"
    assert response.json()["completed"] is True


def test_get_missing_task(client):
    response = client.get(f"/tasks/{uuid4()}")
    assert_error(response, 404)


def test_complete_task(client, task):
    response = client.patch(f"/tasks/{task['id']}", json={"completed": True})

    assert response.status_code == 200
    updated_task = response.json()
    assert updated_task == {**task, "completed": True}
    # Check a second request as well: the change needs to be stored.
    assert client.get(f"/tasks/{task['id']}").json() == updated_task


def test_patch_title_and_clear_description(client, task):
    response = client.patch(
        f"/tasks/{task['id']}", json={"title": "  Ready  ", "description": None}
    )

    assert response.status_code == 200
    assert response.json() == {**task, "title": "Ready", "description": None}


def test_empty_patch_keeps_task_unchanged(client, task):
    response = client.patch(f"/tasks/{task['id']}", json={})
    assert response.status_code == 200
    assert response.json() == task


def test_list_tasks_and_filter_by_completion(client, task):
    completed = client.post(
        "/tasks", json={"title": "Finished task", "completed": True}
    ).json()
    assert completed["id"] != task["id"]

    response = client.get("/tasks")
    assert response.status_code == 200
    assert response.json()["items"] == [task, completed]
    assert response.json()["total"] == 2

    response = client.get("/tasks", params={"completed": True})
    assert response.status_code == 200
    assert response.json()["items"] == [completed]
    assert response.json()["total"] == 1

    response = client.get("/tasks", params={"completed": False})
    assert response.status_code == 200
    assert response.json()["items"] == [task]
    assert response.json()["total"] == 1


def test_list_starts_empty(client):
    response = client.get("/tasks")
    assert response.status_code == 200
    assert response.json() == {
        "total": 0,
        "items": [],
        "limit": 10,
        "offset": 0,
        "next_offset": None,
        "previous_offset": None,
    }


def test_delete_task(client, task):
    response = client.delete(f"/tasks/{task['id']}")

    assert response.status_code == 204
    assert response.content == b""
    assert_error(client.get(f"/tasks/{task['id']}"), 404)
    assert client.get("/tasks").json()["items"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": ""},
        {"title": "   "},
        {"title": None},
        {"title": 42},
        {"title": "Valid", "unexpected": "field"},
    ],
)
def test_create_rejects_invalid_input(client, payload):
    assert_error(client.post("/tasks", json=payload), 422)
    assert client.get("/tasks").json()["items"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"title": ""},
        {"title": "  "},
        {"title": None},
        {"completed": None},
        {"title": "Changed", "unexpected": "field"},
    ],
)
def test_patch_rejects_invalid_input_without_changing_task(client, task, payload):
    response = client.patch(f"/tasks/{task['id']}", json=payload)

    assert_error(response, 422)
    # Validation should happen before any part of an update is saved.
    assert client.get(f"/tasks/{task['id']}").json() == task


@pytest.mark.parametrize("method", ["get", "patch", "delete"])
def test_invalid_task_id_returns_json_error(client, method):
    kwargs = {"json": {"completed": True}} if method == "patch" else {}
    response = client.request(method, "/tasks/not-a-uuid", **kwargs)
    assert_error(response, 422)


@pytest.mark.parametrize("method", ["patch", "delete"])
def test_cannot_update_or_delete_missing_task(client, method):
    kwargs = {"json": {"completed": True}} if method == "patch" else {}
    response = client.request(method, f"/tasks/{uuid4()}", **kwargs)
    assert_error(response, 404)


def test_invalid_filter_returns_json_error(client):
    assert_error(client.get("/tasks?completed=maybe"), 422)


def test_malformed_json_returns_json_error(client):
    response = client.post(
        "/tasks", content='{"title":', headers={"Content-Type": "application/json"}
    )
    assert_error(response, 422)


def test_unknown_route_has_custom_error(client):
    response = client.get("/this-route-does-not-exist")
    assert_error(response, 404)
    assert response.json()["detail"] != "Not Found"


def test_unsupported_method_preserves_allow_header(client):
    response = client.put("/tasks")
    assert_error(response, 405)
    # Clients can use this header to discover which methods are supported.
    assert response.headers.get("allow")


def test_unexpected_error_returns_safe_json(monkeypatch):
    def broken_store():
        raise RuntimeError("Private storage failure details")

    monkeypatch.setitem(app.dependency_overrides, get_task_store, broken_store)
    # Normally TestClient re-raises server errors to help debug tests. Turn that
    # off here so we can inspect the response an actual HTTP client would see.
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/tasks")

    assert_error(response, 500)
    assert response.json() == {"detail": "Internal server error."}


def test_default_page_contains_at_most_ten_tasks(client):
    tasks = [
        client.post("/tasks", json={"title": f"Task {number}"}).json()
        for number in range(11)
    ]

    response = client.get("/tasks")
    assert response.status_code == 200
    assert response.json() == {
        "total": 11,
        "items": tasks[:10],
        "limit": 10,
        "offset": 0,
        "next_offset": 10,
        "previous_offset": None,
    }


def test_following_page_offsets_returns_every_task_once(client):
    tasks = [
        client.post("/tasks", json={"title": f"Task {number}"}).json()
        for number in range(5)
    ]

    # Follow the API's navigation values, as a real client would.
    first = client.get("/tasks", params={"limit": 2}).json()
    middle = client.get(
        "/tasks", params={"limit": 2, "offset": first["next_offset"]}
    ).json()
    last = client.get(
        "/tasks", params={"limit": 2, "offset": middle["next_offset"]}
    ).json()

    assert first["items"] + middle["items"] + last["items"] == tasks
    assert [len(page["items"]) for page in [first, middle, last]] == [2, 2, 1]
    assert [page["total"] for page in [first, middle, last]] == [5, 5, 5]
    assert [page["offset"] for page in [first, middle, last]] == [0, 2, 4]
    assert [page["previous_offset"] for page in [first, middle, last]] == [None, 0, 2]
    assert last["next_offset"] is None


def test_filter_is_applied_before_pagination(client):
    tasks = [
        client.post(
            "/tasks", json={"title": f"Task {number}", "completed": completed}
        ).json()
        for number, completed in enumerate([True, False, True, False, True, True])
    ]

    response = client.get(
        "/tasks", params={"completed": True, "limit": 2, "offset": 1}
    )
    assert response.status_code == 200
    assert response.json() == {
        "total": 4,
        "items": [tasks[2], tasks[4]],
        "limit": 2,
        "offset": 1,
        "next_offset": 3,
        "previous_offset": 0,
    }


def test_offset_past_last_task_returns_empty_page(client, task):
    response = client.get("/tasks", params={"limit": 2, "offset": 99})
    assert response.status_code == 200
    assert response.json() == {
        "total": 1,
        "items": [],
        "limit": 2,
        "offset": 99,
        "next_offset": None,
        "previous_offset": 97,
    }


@pytest.mark.parametrize("limit", [1, 100])
def test_page_size_boundaries_are_allowed(client, task, limit):
    response = client.get("/tasks", params={"limit": limit})

    assert response.status_code == 200
    page = response.json()
    assert page["items"] == [task]
    assert page["limit"] == limit
    assert page["total"] == 1
    # A page filled exactly to its limit still has no next page here.
    assert page["next_offset"] is None


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": 101},
        {"limit": "many"},
        {"offset": -1},
        {"offset": "later"},
    ],
)
def test_invalid_pagination_returns_json_error(client, params):
    assert_error(client.get("/tasks", params=params), 422)


def test_create_and_delete_append_timestamped_events(client):
    main.LOG_FILE.write_text("existing log entry\n", encoding="utf-8")
    before = datetime.now(timezone.utc)

    created = client.post("/tasks", json={"title": "Check the background job"})
    assert created.status_code == 201
    task_id = created.json()["id"]
    deleted = client.delete(f"/tasks/{task_id}")
    assert deleted.status_code == 204
    assert deleted.content == b""
    after = datetime.now(timezone.utc)

    # TestClient waits for background tasks, so no sleeps or polling are needed.
    log_text = main.LOG_FILE.read_text(encoding="utf-8")
    assert log_text.endswith("\n")
    lines = log_text.splitlines()
    assert lines[0] == "existing log entry"
    events = [line.split() for line in lines[1:]]
    assert [(action, logged_id) for _, action, logged_id in events] == [
        ("created", task_id),
        ("deleted", task_id),
    ]
    for timestamp, _, _ in events:
        logged_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        assert logged_at.utcoffset() == timedelta(0)
        assert before <= logged_at <= after


def test_reads_updates_and_failed_requests_do_not_add_events(client, task):
    original_log = main.LOG_FILE.read_text(encoding="utf-8")

    assert client.get("/tasks").status_code == 200
    assert client.get(f"/tasks/{task['id']}").status_code == 200
    updated = client.patch(f"/tasks/{task['id']}", json={"completed": True})
    assert updated.status_code == 200
    assert_error(client.post("/tasks", json={"title": " "}), 422)
    assert_error(client.delete(f"/tasks/{uuid4()}"), 404)
    assert_error(client.delete("/tasks/not-a-uuid"), 422)

    assert main.LOG_FILE.read_text(encoding="utf-8") == original_log


def test_log_write_failure_does_not_undo_successful_requests(
    client, monkeypatch, tmp_path, caplog
):
    # Opening a directory as a file fails reliably, even with broad file permissions.
    monkeypatch.setattr(main, "LOG_FILE", tmp_path)
    with caplog.at_level("ERROR", logger=main.__name__):
        created = client.post("/tasks", json={"title": "Still save this task"})
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert client.get(f"/tasks/{task_id}").status_code == 200
        deleted = client.delete(f"/tasks/{task_id}")
        assert deleted.status_code == 204
        assert deleted.content == b""
        assert_error(client.get(f"/tasks/{task_id}"), 404)

    errors = [record for record in caplog.records if record.name == main.__name__]
    assert len(errors) == 2
    assert all(record.levelname == "ERROR" for record in errors)
    assert all(
        record.exc_info and isinstance(record.exc_info[1], OSError) for record in errors
    )
