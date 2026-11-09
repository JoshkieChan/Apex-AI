# ADR 0006 — RS256 bearer identity

## Status

Accepted

## Decision

Protected routes resolve a principal from an RS256 JWT. The validator fixes the algorithm, issuer, audience, and time claims. The subject must already be mapped in `identity_subjects`. Development signing is a separate function and route that production configuration cannot enable. Readiness fails closed when that configuration is absent. Liveness does not report the process as authenticated.

## Why

The development header let the caller choose a principal. A bearer token validated with a pinned public key gives tests a local issuer and leaves room for an external issuer without coupling the runtime to a vendor SDK. JWKS fetching is deferred so this milestone does not add a network trust dependency.

## Consequences

Production needs `APEX_AUTH_ISSUER`, `APEX_AUTH_AUDIENCE`, and `APEX_AUTH_PUBLIC_KEY_PEM`. It must not have the private key. Key rotation is a configuration change, not an automatic JWKS refresh. `POST /v1/dev/tokens` is a local convenience and is 404 when development auth is off.
