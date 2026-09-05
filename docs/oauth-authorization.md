# OAuth authorization

OAuth login requires an explicit allowlist of provider user IDs. A valid GitHub
or Google login alone does not grant control-plane access. All authorized users
currently receive the same administrative permissions.

Set these together before starting the server:

```sh
AGENTHANDLER_OAUTH_PROVIDER=github
AGENTHANDLER_OAUTH_CLIENT_ID=your-client-id
AGENTHANDLER_OAUTH_CLIENT_SECRET=your-client-secret
AGENTHANDLER_OAUTH_ALLOWED_SUBJECTS=16549185,another-provider-user-id
```

Use the stable `id` returned by the configured provider's user-info endpoint
(GitHub `/user`; Google OAuth v2 `/userinfo`), not a username, email, organization,
or domain. IDs are exact strings, comma separated. For direct route registration,
pass `allowed_subjects=["16549185"]` to `register_oauth_routes`.

Migration: existing OAuth deployments must add this allowlist. Missing subjects
or incomplete OAuth configuration now prevent startup. Unlisted users receive
403 without a session token. API-key-only and intentionally unauthenticated
local deployments retain their existing behavior.

OAuth sessions remain app-local and expire after 24 hours. Restart the app to
apply allowlist changes and invalidate existing sessions. Shared session storage,
per-user roles, and organization membership checks are not implemented.
