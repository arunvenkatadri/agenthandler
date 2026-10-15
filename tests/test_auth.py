"""Authentication regressions: challenges, sessions, and transport parity."""

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agenthandler import auth
from agenthandler.server import create_app
from agenthandler.session import SessionManager
from agenthandler.store import MemoryStore


@pytest.fixture(autouse=True)
def clean_auth_env(monkeypatch):
    for name in ("API_KEY", "OAUTH_PROVIDER", "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET"):
        monkeypatch.delenv(f"AGENTHANDLER_{name}", raising=False)


def make_client(**kwargs):
    manager = SessionManager(MemoryStore())
    sid = manager.start("auth-test")
    return TestClient(create_app(manager, **kwargs)), sid


@pytest.fixture
def oauth_client(monkeypatch):
    monkeypatch.setenv("AGENTHANDLER_OAUTH_PROVIDER", "google")
    monkeypatch.setenv("AGENTHANDLER_OAUTH_CLIENT_ID", "client&special")
    monkeypatch.setenv("AGENTHANDLER_OAUTH_CLIENT_SECRET", "secret")
    return make_client(require_auth=True)


@pytest.fixture
def provider(monkeypatch):
    client = AsyncMock()
    client.post.return_value = httpx.Response(200, json={"access_token": "provider-token"})
    client.get.return_value = httpx.Response(200, json={"email": "person@example.com"})
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr(httpx, "AsyncClient", lambda: context)
    return client


def login(client):
    state = client.get("/auth/login").json()["state"]
    return client.get("/auth/callback", params={"state": state, "code": "code"})


@pytest.mark.parametrize("token", [None, "__REJECT_ALL__", "random"])
def test_unconfigured_required_auth_rejects_every_token(token):
    client, sid = make_client(require_auth=True)
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    assert client.get("/sessions", headers=headers).status_code == 401
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/sessions/{sid}/events", params={"token": token or ""}):
            pass
    assert exc.value.code == 4001


def test_login_state_cannot_authenticate(oauth_client):
    client, sid = oauth_client
    state = client.get("/auth/login").json()["state"]
    for token in (state, f"_state_{state}"):
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/sessions", headers=headers).status_code == 401
        assert client.get("/auth/me", headers=headers).status_code == 401
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/sessions/{sid}/events", params={"token": token}):
                pass
        assert exc.value.code == 4001


@pytest.mark.parametrize("require_auth", [False, True])
def test_oauth_websocket_rejects_anonymous(oauth_client, require_auth):
    client, sid = make_client(require_auth=require_auth)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/sessions/{sid}/events"):
            pass
    assert exc.value.code == 4001


def test_successful_login_authenticates_http_and_websocket(oauth_client, provider):
    client, sid = oauth_client
    response = login(client)
    assert response.status_code == 200
    token = response.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/sessions", headers=headers).status_code == 200
    assert client.get("/auth/me", headers=headers).json()["user"] == "person@example.com"
    with client.websocket_connect(f"/sessions/{sid}/events", params={"token": token}) as ws:
        assert ws.receive_json()["type"] == "audit"
    assert (
        provider.post.call_args.kwargs["data"]["redirect_uri"] == "http://testserver/auth/callback"
    )
    assert "agenthandler_oauth_state" not in client.cookies


def test_oauth_sessions_are_app_local(oauth_client, provider):
    client, _ = oauth_client
    token = login(client).json()["token"]
    other, _ = make_client(require_auth=True)
    headers = {"Authorization": f"Bearer {token}"}
    assert other.get("/sessions", headers=headers).status_code == 401
    assert other.get("/auth/me", headers=headers).status_code == 401


def test_state_is_bound_to_browser_and_single_use(oauth_client, provider):
    client, _ = oauth_client
    state = client.get("/auth/login").json()["state"]
    cookie = client.cookies.get("agenthandler_oauth_state")
    client.cookies.clear()
    params = {"state": state, "code": "code"}
    assert client.get("/auth/callback", params=params).status_code == 400
    provider.post.assert_not_called()
    client.cookies.set("agenthandler_oauth_state", cookie)
    assert client.get("/auth/callback", params=params).status_code == 200
    client.cookies.set("agenthandler_oauth_state", cookie)
    assert client.get("/auth/callback", params=params).status_code == 400
    assert provider.post.call_count == 1


def test_state_expiry(oauth_client, provider, monkeypatch):
    client, _ = oauth_client
    state = client.get("/auth/login").json()["state"]
    now = auth.time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + 601)
    assert client.get("/auth/callback", params={"state": state, "code": "code"}).status_code == 400
    provider.post.assert_not_called()


def test_session_expiry(oauth_client, provider, monkeypatch):
    client, sid = oauth_client
    token = login(client).json()["token"]
    now = auth.time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + 86401)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/auth/me", headers=headers).status_code == 401
    assert client.get("/sessions", headers=headers).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/sessions/{sid}/events", params={"token": token}):
            pass


def test_login_url_encodes_parameters(oauth_client):
    client, _ = oauth_client
    response = client.get("/auth/login")
    params = parse_qs(urlparse(response.json()["login_url"]).query)
    assert params["client_id"] == ["client&special"]
    assert params["scope"] == ["openid email profile"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]


@pytest.mark.parametrize("phase", ["token", "user", "missing_token"])
def test_provider_failure_does_not_issue_session(oauth_client, provider, phase):
    client, _ = oauth_client
    if phase == "token":
        provider.post.return_value = httpx.Response(401)
    elif phase == "user":
        provider.get.return_value = httpx.Response(401)
    else:
        provider.post.return_value = httpx.Response(200, json={"error": "invalid_grant"})
    assert login(client).status_code == 401


def test_api_key_websocket_and_unicode_rejection():
    client, sid = make_client(api_key="secret")
    with client.websocket_connect(f"/sessions/{sid}/events?token=secret") as ws:
        assert ws.receive_json()["type"] == "audit"
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/sessions/{sid}/events", params={"token": "🔑"}):
            pass
    assert exc.value.code == 4001
