"""Router-level authentication dependencies for BASTION.

Replaces the old BaseHTTPMiddleware approach with FastAPI Security()
dependencies applied per-router. Benefits: contextvars propagate correctly,
streaming responses aren't buffered, no fragile path-exclusion lists.
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from bastion.models import A2AConfig, AuthConfig

logger = logging.getLogger(__name__)

# Security scheme definitions with unique scheme_names for OpenAPI
_admin_api_key_header = APIKeyHeader(
    name="Authorization",
    scheme_name="AdminAPIKey",
    auto_error=False,
)

_a2a_bearer = HTTPBearer(
    scheme_name="A2ABearerToken",
    auto_error=False,
)


def make_admin_key_dependency(config: AuthConfig):
    """Create a dependency that validates admin API keys.

    Returns a dependency function that checks the Authorization header
    for a valid Bearer token from the configured api_keys list.
    When auth is disabled or no keys configured, passes through.
    """
    valid_keys = frozenset(config.api_keys)

    async def verify_admin_key(
        request: Request,
        api_key: str | None = Depends(_admin_api_key_header),
    ) -> str | None:
        # Skip auth when disabled or no keys configured
        if not config.enabled or not valid_keys:
            return None

        if not api_key:
            raise HTTPException(status_code=401, detail="Missing Authorization header")

        # Parse "Bearer <token>" format
        parts = api_key.split(" ", maxsplit=1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(
                status_code=401,
                detail="Invalid Authorization header format, expected 'Bearer <token>'",
            )

        token = parts[1]
        if token not in valid_keys:
            logger.warning("Rejected invalid API key for %s", request.url.path)
            raise HTTPException(status_code=401, detail="Invalid API key")

        # Store validated identity for downstream use
        request.state.admin_authenticated = True
        request.state.admin_token = token
        return token

    return verify_admin_key


def make_a2a_token_dependency(config: A2AConfig):
    """Create a dependency that validates A2A bearer tokens.

    Returns a dependency function that checks for a valid bearer token
    from the configured A2A tokens list. When no tokens configured,
    passes through (open access).
    """
    valid_tokens = frozenset(config.tokens)

    async def verify_a2a_token(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_a2a_bearer),  # noqa: B008
    ) -> str | None:
        # No tokens configured = open access
        if not valid_tokens:
            return None

        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="Missing or invalid Authorization header",
            )

        if credentials.credentials not in valid_tokens:
            raise HTTPException(status_code=401, detail="Invalid A2A token")

        # Store validated identity
        request.state.a2a_authenticated = True
        request.state.a2a_token = credentials.credentials
        return credentials.credentials

    return verify_a2a_token
