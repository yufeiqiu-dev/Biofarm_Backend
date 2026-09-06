"""Resolving an order's `user_id` back to the person who placed it.

`Order.user_id` is a Cognito `sub` - a uuid that identifies the account and
nothing a human can read. That was tolerable while `customer_email` always held
the account's own address, because the admin console could show that instead.
It no longer does: a customer may nominate where order mail goes, so a lab
ordering against `purchasing@lab.edu` leaves nothing on the order pointing at
the account that actually placed it.

**This is the only part of the application that calls a Cognito API.** Token
verification reads the pool's JWKS document, which is public HTTPS and needs no
credentials at all; `AdminGetUser` is a signed call, so the deployed role needs
`cognito-idp:AdminGetUser` on the pool.

`AdminGetUser` rather than `ListUsers`, deliberately. Neither can be scoped below
the pool in IAM, but they grant very different things: `ListUsers` lets whatever
holds the credential enumerate every account, while this reads only an account
whose sub the caller already has. The difference is the customer list.

It works because `AdminGetUser` takes a `Username` and this pool has
`UsernameAttributes: ["email"]` with no alias attributes - the configuration in
which that value "must be the sub of a local user". **Worth confirming against
the pool before relying on it**: if a pool did not accept a sub there, the call
would raise `UserNotFoundException`, which is indistinguishable from an account
that has genuinely been deleted.

    aws cognito-idp admin-get-user --user-pool-id <pool> --username <a sub>
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from app.core.aws import get_client
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# How long a resolved account is trusted. Short enough that a changed email
# shows up the same session, long enough that opening several orders for one
# customer is one call rather than several.
_CACHE_TTL_SECONDS = 300

_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}

# Validated because it arrives from a URL path and is sent to AWS.
#
# This mattered more under the ListUsers version, where the sub was interpolated
# into a filter expression and a quote could change what that expression meant.
# AdminGetUser takes it as a parameter, so there is nothing left to inject into -
# this now just refuses obvious junk rather than paying for a round trip to
# discover it. Kept because it costs nothing and rejects no legitimate sub: real
# ones are uuids, and the synthetic AUTH_BYPASS identity is "local-dev-user".
_SAFE_SUB = re.compile(r"\A[A-Za-z0-9_.:@-]{1,128}\Z")

# What AUTH_BYPASS hands out in place of a real identity. Nothing in Cognito
# matches it, so resolving it would be a pointless API call that always fails -
# and would make local development need an IAM permission it otherwise does not.
_BYPASS_SUB = "local-dev-user"


def _attributes_of(user: dict[str, Any]) -> dict[str, str]:
    """AdminGetUser returns UserAttributes; ListUsers returned Attributes."""
    return {a["Name"]: a["Value"] for a in user.get("UserAttributes", [])}


def get_account_by_sub(sub: str) -> Optional[dict[str, Any]]:
    """The Cognito account for `sub`, or None when there is no such account.

    Raises ValueError for a sub that cannot be safely queried. Any AWS failure
    propagates - the endpoint decides what a lookup failure means, and silently
    returning None would present "Cognito is unreachable" as "this customer does
    not exist".
    """
    if not _SAFE_SUB.match(sub):
        raise ValueError("Not a well-formed user id")

    settings = get_settings()

    if settings.auth_bypass and sub == _BYPASS_SUB:
        return {
            "sub": sub,
            "username": _BYPASS_SUB,
            "email": "dev@example.com",
            "name": "Local development user",
            "status": "CONFIRMED",
            "enabled": True,
            "created_at": None,
            "synthetic": True,
        }

    cached = _cache.get(sub)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    client = get_client("cognito-idp", settings.cognito_region)
    try:
        user = client.admin_get_user(
            UserPoolId=settings.cognito_user_pool_id,
            Username=sub,
        )
    except client.exceptions.UserNotFoundException:
        # Cached as a miss too. A deleted account is looked up every time one of
        # its orders is opened, and without this each one is a fresh API call
        # that will never start succeeding.
        #
        # Note this is also what a pool that did not accept a sub as the username
        # would raise, which is why that is worth confirming rather than assuming.
        logger.info("no Cognito account for sub %s", sub)
        _cache[sub] = (time.monotonic() + _CACHE_TTL_SECONDS, None)
        return None

    attributes = _attributes_of(user)
    created = user.get("UserCreateDate")

    account = {
        "sub": attributes.get("sub", sub),
        "username": user.get("Username", ""),
        "email": attributes.get("email", ""),
        "name": attributes.get("name") or attributes.get("given_name") or "",
        "status": user.get("UserStatus", ""),
        "enabled": bool(user.get("Enabled", True)),
        "created_at": created.isoformat() if created else None,
        "synthetic": False,
    }

    _cache[sub] = (time.monotonic() + _CACHE_TTL_SECONDS, account)
    return account


def clear_cache() -> None:
    """Drop everything resolved so far. For tests, and for a manual refresh."""
    _cache.clear()
