"""How the application authenticates to AWS.

There are two ways this app runs, and they want different answers:

  - **Locally**, there is an IAM user's access key in `.env`, because a laptop
    has no role to assume.
  - **On App Runner**, there is an instance role. Nothing needs a key at all,
    and supplying one would be worse than not: a long-lived credential in an
    environment variable is a credential that can leak, be copied between
    environments, and outlive the person who made it. The role's credentials are
    short-lived and rotate on their own.

So the keys are optional, and boto3 is told about them only when they exist.
With them absent, boto3 falls through its default provider chain and picks up
the container's role - which is the whole reason the instance role is granted
S3 and SES access in the first place.

This mattered more than a preference: the App Runner stack sets neither key, and
the settings that read them used to be required, so the container could not
start at all. It failed validation, exited, and never passed a health check.
"""

from __future__ import annotations

from functools import lru_cache

import boto3

from app.core.config import get_settings


@lru_cache
def get_client(service_name: str, region_name: str | None = None):
    """A boto3 client for `service_name`, cached.

    Cached because boto3 parses the full service model on every `client()` call,
    which is tens of milliseconds paid on every presign, every delete and every
    message for no reason. The clients used here are thread-safe.

    `region_name` overrides the default for services that do not live in
    `aws_region`. The user pool has its own `cognito_region` setting and there is
    no reason the two must agree - resolving a sub against the wrong region
    silently finds nobody, which reads as "no such account" rather than as a
    misconfiguration.
    """
    settings = get_settings()

    kwargs = {"region_name": region_name or settings.aws_region}

    # Only when there is something to pass. Handing boto3 an empty string is not
    # the same as handing it nothing: it would take the empty credential as the
    # answer and never consult the role.
    if settings.aws_access_key_id:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key.get_secret_value()

    return boto3.client(service_name, **kwargs)
