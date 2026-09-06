from typing import Optional

from pydantic import BaseModel


class AccountOut(BaseModel):
    """A Cognito account, as much of it as the admin console needs.

    Deliberately not the raw ListUsers shape: that carries every custom
    attribute the pool happens to define, and this is rendered on a page whose
    only question is "who is this order for".
    """

    sub: str
    username: str
    email: str
    name: str
    status: str
    enabled: bool
    created_at: Optional[str] = None
    # True for the identity AUTH_BYPASS invents, so the console can say so
    # rather than implying a real account was found.
    synthetic: bool = False
