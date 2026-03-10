# Phase 2: Authentication — NextAuth.js + Keycloak

## Goal

Users log in via Keycloak before accessing the chat. User identity (sub, email, name) is available in the app for per-user features (thread isolation, usage tracking, future rate limiting). This requires forking deep-agents-ui to add NextAuth.js with Keycloak as the OIDC provider.

## Prerequisites

Phase 1 must be complete:
- Deep agent with MCP tools working
- LangGraph Server serving the agent API
- deep-agents-ui rendering chat, streaming, and thread history

## What You're Building

1. A fork of deep-agents-ui with NextAuth.js + Keycloak OIDC
2. Protected routes — unauthenticated users redirected to Keycloak login
3. User identity available in the UI (display name, user ID)
4. Auth headers passed to LangGraph Server API calls

## What You're NOT Building

- No custom login page (Keycloak provides the login UI)
- No user management UI (Keycloak handles user admin)
- No role-based access control (all authenticated users have equal access)
- No per-user agent configuration (agents are defined in code)

## Key Packages (JavaScript / Next.js)

| Package | Purpose |
|---------|---------|
| `next-auth` (Auth.js) | Authentication framework for Next.js |
| Keycloak provider | Built into next-auth, OIDC provider for Keycloak |

## Implementation Details

### Fork deep-agents-ui

Create a fork of `langchain-ai/deep-agents-ui` in the Rhiza GitHub org. This becomes the maintained frontend repo.

### Add NextAuth.js

NextAuth.js has a built-in Keycloak provider. Configuration requires:
- `KEYCLOAK_CLIENT_ID` — OIDC client ID
- `KEYCLOAK_CLIENT_SECRET` — OIDC client secret
- `KEYCLOAK_ISSUER` — Keycloak realm URL (e.g., `http://keycloak:8080/realms/sheerwater`)
- `NEXTAUTH_URL` — The app's public URL
- `NEXTAUTH_SECRET` — Session encryption key

The Keycloak OIDC client should be configured as "confidential" access type with the redirect URI pointing to the NextAuth.js callback endpoint.

### Protect Routes

Wrap pages/layouts with NextAuth.js session checks. Unauthenticated users get redirected to Keycloak. After login, they return to the app with a session containing their identity.

### Dual URL Strategy

Same pattern as sheerwater-chat: the Next.js backend (server-side) talks to Keycloak via the Docker-internal URL (`http://keycloak:8080`), while the browser redirects use the public URL (`http://localhost:8180`). NextAuth.js handles this via the `issuer` configuration.

### Pass User Identity to LangGraph Server

When the UI makes API calls to LangGraph Server, include user identity in the request headers or as metadata. This enables:
- Per-user thread isolation (threads tagged with user ID)
- Future: usage tracking, rate limiting

How exactly to pass user context depends on LangGraph Server's API — check if it supports custom headers or metadata on thread/run creation.

### Keycloak Realm

Reuse the existing `sheerwater` realm from Docker Compose (same Keycloak instance as sheerwater-chat). Add a new OIDC client `rhiza-agents` with:
- Client protocol: openid-connect
- Access type: confidential
- Valid redirect URIs: `http://localhost:3000/*` (dev), production URL later
- Client secret: generated or set to a dev value

The `keycloak/realm.json` file should include this client for auto-import on startup.

## Docker Compose Changes

Update the deep-agents-ui service to use the forked image instead of upstream. Add the NextAuth.js environment variables:
- `KEYCLOAK_CLIENT_ID`
- `KEYCLOAK_CLIENT_SECRET`
- `KEYCLOAK_ISSUER` (internal Keycloak URL for server-side)
- `NEXTAUTH_URL`
- `NEXTAUTH_SECRET`

## Acceptance Criteria

1. Open `http://localhost:3000` → redirected to Keycloak login
2. Create a user in Keycloak dev console, log in
3. After login, see the chat UI with user name displayed
4. Chat works the same as Phase 1 (MCP tools, streaming, thread history)
5. Open an incognito window → must log in again (no shared session)
6. Logout works (clears session, redirects to login)

## What NOT to Do

- Do not build a custom login page — Keycloak provides the login UI
- Do not add user registration — Keycloak handles this (or users are pre-created by admin)
- Do not implement role-based permissions — all authenticated users are equal for now
- Do not add user preferences or settings storage — that's out of scope
