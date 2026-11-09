# Remote trust is configured, not discovered

Apex can verify RS256 tokens from a configured JWKS URL and can call a registered remote MCP server. Neither path trusts a URL, key, or tool list that arrived from the caller or the remote service.

## Decision

Identity verification uses the configured issuer, audience, and either a PEM public key or a JWKS URL. When the JWKS URL is set, a failed fetch or an unknown key is authentication failure. The PEM is not a fallback. Tokens cannot supply `jku`, `x5u`, or `jwk`. There is no OpenID Connect discovery.

Remote MCP servers are registered explicitly. The endpoint is checked again at connection time. Loopback, private, link-local, and reserved addresses are rejected unless the process is outside production and `APEX_MCP_ALLOW_LOCAL_FIXTURE` is set, and then only a literal loopback HTTP URL is allowed. Credentials are `env:NAME` references. Discovery does not register a capability or grant a permission.

Uncertain non-idempotent work stays uncertain until an authorized reconciliation read. The worker does not retry it. Reconciliation of the local fixture uses the fixture store, not the tool's success flag and not a model note.

Migrations stay a one-shot command. API replicas do not run Alembic on startup. Production readiness requires revision `0003_external_trust`.

## Consequences

Local tests can use an HTTP JWKS fixture and a local HTTPS MCP fixture. Production startup rejects those exceptions. A remote MCP server that Apex cannot read independently remains inconclusive after a lost response. That is intentional.
