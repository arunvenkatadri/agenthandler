"""Authenticated application workflow, using real disk-backed stores."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from agenthandler.workbench import create_workbench_app, report_template, summarize_orders

AUTH = {"Authorization": "Bearer test-only-key"}
ORDERS = "item,quantity,unit_price\nNotebook,2,4.50\nPen,3,1.20"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_workbench_app(str(tmp_path), "test-only-key")) as client:
        yield client


def test_authenticated_workflow_persists_verifiable_report(client, tmp_path):
    assert client.get("/").status_code == 200
    assert client.get("/tasks").status_code == 401
    assert client.post("/tasks", json={"template_id": "order-report"}).status_code == 401
    created = client.post(
        "/tasks",
        headers=AUTH,
        json={
            "template_id": "order-report",
            "inputs": {"orders": ORDERS},
        },
    )
    assert created.status_code == 201
    task = created.json()
    assert task["template_id"] == "order-report"
    task_id = task["task_id"]
    assert client.post(f"/tasks/{task_id}/run").status_code == 401
    result = client.post(f"/tasks/{task_id}/run", headers=AUTH).json()
    assert result["status"] == "verified"
    assert result["calls_reserved"] == 4
    session = client.get(f"/sessions/{task['session_id']}", headers=AUTH).json()
    assert session["status"] == "stopped"
    assert result["tokens_reserved"] == result["cost_reserved_microusd"] == 0
    assert result["milestones"]["publish"]["output"]["report"]["total_cents"] == 1260
    assert len(list((tmp_path / "reports").glob("*.json"))) == 1
    with TestClient(create_workbench_app(str(tmp_path), "test-only-key")) as fresh:
        restored = fresh.get(f"/tasks/{task_id}", headers=AUTH).json()
        assert restored == result
        assert fresh.get("/tasks", headers=AUTH).json() == [result]
        assert fresh.post(f"/tasks/{task_id}/run", headers=AUTH).json() == result


@pytest.mark.parametrize(
    "orders",
    [
        "",
        "wrong,header",
        "item,quantity,unit_price\n",
        "item,quantity,unit_price\nX,-1,1",
        "item,quantity,unit_price\nX,1,NaN",
        "item,quantity,unit_price\nX,1,0.001",
        "item,quantity,unit_price\nX,1,1e-999999999",
        "item,quantity,unit_price\nX,1,Infinity",
        "item,quantity,unit_price\n,1,1",
        "item,quantity,unit_price\nX,1",
        "item,quantity,unit_price\nX,1,1,extra",
        "item,quantity,unit_price\nX,1,1000001",
        "item,quantity,unit_price\nX,1000001,1",
        "item,quantity,unit_price\nX,not-a-number,1",
        "x" * 10001,
    ],
)
def test_invalid_input_has_no_side_effects(client, orders):
    response = client.post(
        "/tasks",
        headers=AUTH,
        json={
            "template_id": "order-report",
            "inputs": {"orders": orders},
        },
    )
    assert response.status_code == 422
    assert client.get("/tasks", headers=AUTH).json() == []
    assert client.get("/sessions", headers=AUTH).json() == []


def test_unknown_resources_and_templates(client):
    assert client.get("/tasks/missing", headers=AUTH).status_code == 404
    assert client.post("/tasks/missing/run", headers=AUTH).status_code == 404
    assert client.post("/tasks", headers=AUTH, json={"template_id": "shell"}).status_code == 404
    assert client.get("/task-templates", headers=AUTH).json()[0]["id"] == "order-report"


def test_client_cannot_raise_server_budget(client):
    response = client.post(
        "/tasks",
        headers=AUTH,
        json={
            "template_id": "order-report",
            "limits": {"max_calls": 999999},
            "inputs": {"orders": ORDERS, "max_tokens": 999999},
        },
    )
    assert response.json()["limits"]["max_tokens"] == 0
    assert response.json()["limits"]["max_calls"] == 12
    assert response.json()["inputs"] == {"orders": ORDERS}


def test_server_rejects_changed_template(client, tmp_path):
    task = client.post(
        "/tasks",
        headers=AUTH,
        json={
            "template_id": "order-report",
            "inputs": {"orders": ORDERS},
        },
    ).json()
    # Persisting arbitrary task records does not give a client a way to execute
    # unregistered code; the restored server owns the callback registry.
    runner = client.app.state.task_runner
    template = report_template(tmp_path)
    changed = [replace(template.milestones[0], version="2"), template.milestones[1]]
    import asyncio

    with pytest.raises(ValueError, match="specification changed"):
        asyncio.run(runner.run(task["task_id"], changed))


def test_exact_currency_arithmetic():
    result = summarize_orders({"orders": "item,quantity,unit_price\nX,3,0.10"})
    assert result["total_cents"] == 30
