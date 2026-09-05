"""Authentication providers for the REST control plane.

Supports:
- API key (Bearer token) — simple, self-issued
- OAuth2 (GitHub, Google) — for production deployments

Usage:
    # API key only
    app = create_app(api_key="my-secret")

    # OAuth2 via env vars:
    #   AGENTHANDLER_OAUTH_PROVIDER=github
    #   AGENTHANDLER_OAUTH_CLIENT_ID=...
    #   AGENTHANDLER_OAUTH_CLIENT_SECRET=...
    app = create_app()
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

try:
    from fastapi import Depends, FastAPI, HTTPException, Request, Response
    from fastapi.security import (
        HTTPAuthorizationCredentials,
        HTTPBearer,
    )
except ImportError:
    pass


_bearer_scheme = HTTPBearer(auto_error=False)


def make_auth_dependency(
    api_key: Optional[str] = None,
    oauth_enabled: bool = False,
    require_auth: bool = False,
    oauth_tokens: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Any:
    """Build a FastAPI dependency that enforces auth.

    Checks in order:
    1. Bearer token matches api_key (if set)
    2. Bearer token is a valid OAuth session token (if oauth enabled)
    3. Reject

    With no configured provider, requests pass only if require_auth is False.
    """
    sessions = oauth_tokens if oauth_tokens is not None else {}

    async def _check_auth(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    ) -> Optional[Dict[str, Any]]:
        # No auth configured — open access
        if api_key is None and not oauth_enabled and not require_auth:
            return None

        if credentials is None:
            raise HTTPException(status_code=401, detail="Missing authentication")

        token = credentials.credentials

        # Check API key
        if api_key is not None and secrets.compare_digest(token.encode(), api_key.encode()):
            return {"auth_type": "api_key"}

        # Check OAuth session token
        if oauth_enabled:
            session = sessions.get(token)
            if (
                session is not None
                and session.get("auth_type") == "oauth"
                and session.get("expires_at", 0) > time.time()
            ):
                return session
            sessions.pop(token, None)

        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    return _check_auth


def register_oauth_routes(
    app: "FastAPI",
    provider: str,
    client_id: str,
    client_secret: str,
    oauth_tokens: Optional[Dict[str, Dict[str, Any]]] = None,
    allowed_subjects: Optional[List[str]] = None,
) -> None:
    """Add OAuth2 login/callback routes to the FastAPI app.

    Supports: "github", "google"
    """
    providers = {
        "github": {
            "authorize_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "user_url": "https://api.github.com/user",
            "scope": "read:user",
        },
        "google": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "user_url": "https://www.googleapis.com/oauth2/v2/userinfo",
            "scope": "openid email profile",
        },
    }

    if provider not in providers:
        raise ValueError(f"Unsupported OAuth provider: {provider}. Use: {list(providers.keys())}")

    subjects = frozenset(subject.strip() for subject in (allowed_subjects or []) if subject.strip())
    if not subjects:
        raise ValueError("OAuth requires a non-empty allowed_subjects list of provider user IDs")

    config = providers[provider]
    sessions = oauth_tokens if oauth_tokens is not None else {}
    # Login challenges are not credentials and are scoped to this app/browser.
    states: Dict[str, Dict[str, Any]] = {}
    state_lock = threading.Lock()
    cookie_name = "agenthandler_oauth_state"

    @app.get("/auth/login")
    def oauth_login(request: Request, response: Response) -> Dict[str, Any]:
        """Redirect URL for OAuth2 login."""
        callback_url = str(request.base_url) + "auth/callback"
        state = secrets.token_hex(16)
        with state_lock:
            for expired in [k for k, v in states.items() if v["expires_at"] <= time.time()]:
                del states[expired]
            states[state] = {"expires_at": time.time() + 600, "redirect_uri": callback_url}
        response.set_cookie(
            cookie_name,
            state,
            max_age=600,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/auth",
        )
        url = (
            config["authorize_url"]
            + "?"
            + urlencode(
                {
                    "client_id": client_id,
                    "redirect_uri": callback_url,
                    "scope": config["scope"],
                    "state": state,
                }
            )
        )
        return {"login_url": url, "state": state}

    @app.get("/auth/callback")
    async def oauth_callback(
        request: Request, response: Response, code: str, state: str = ""
    ) -> Dict[str, Any]:
        """OAuth2 callback — exchange code for token, create session."""
        # Verify the state parameter matches one we issued
        browser_state = request.cookies.get(cookie_name, "")
        if not state or not secrets.compare_digest(state.encode(), browser_state.encode()):
            raise HTTPException(status_code=400, detail="Invalid OAuth state cookie")
        with state_lock:
            state_record = states.pop(state, None)
        if state_record is None or state_record.get("expires_at", 0) <= time.time():
            raise HTTPException(
                status_code=400,
                detail="Invalid or expired OAuth state parameter",
            )
        try:
            import httpx
        except ImportError:
            raise HTTPException(
                status_code=500,
                detail="httpx required for OAuth. pip install httpx",
            )

        # Exchange code for access token
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                config["token_url"],
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": state_record["redirect_uri"],
                },
                headers={"Accept": "application/json"},
            )
            if token_resp.status_code != 200:
                raise HTTPException(status_code=401, detail="OAuth token exchange failed")
            token_data = token_resp.json()
            if not isinstance(token_data, dict):
                raise HTTPException(status_code=401, detail="Invalid OAuth token response")
            access_token = token_data.get("access_token")
            if not access_token:
                raise HTTPException(status_code=401, detail="No access token in response")

            # Get user info
            user_resp = await client.get(
                config["user_url"],
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_resp.status_code != 200:
                raise HTTPException(status_code=401, detail="Failed to fetch user info")
            user_info = user_resp.json()

        # Authentication alone does not authorize access to this control plane.
        # Both configured providers expose a stable ID in their user-info response.
        subject = user_info.get("id") if isinstance(user_info, dict) else None
        if (
            isinstance(subject, bool)
            or not isinstance(subject, (str, int))
            or str(subject) not in subjects
        ):
            raise HTTPException(status_code=403, detail="OAuth user is not authorized")

        # Create a session token
        session_token = secrets.token_hex(32)
        sessions[session_token] = {
            "auth_type": "oauth",
            "provider": provider,
            "subject": str(subject),
            "user": user_info.get("login") or user_info.get("email", "unknown"),
            "user_info": user_info,
            "expires_at": time.time() + 86400,  # 24 hours
        }

        response.delete_cookie(cookie_name, path="/auth")
        return {
            "token": session_token,
            "user": sessions[session_token]["user"],
            "expires_in": 86400,
        }

    @app.get("/auth/me")
    async def auth_me(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    ) -> Dict[str, Any]:
        """Get info about the current authenticated user."""
        if credentials is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        session = sessions.get(credentials.credentials)
        if (
            session
            and session.get("auth_type") == "oauth"
            and session.get("expires_at", 0) > time.time()
        ):
            return {"user": session["user"], "provider": session.get("provider")}
        raise HTTPException(status_code=401, detail="Invalid or expired token")
