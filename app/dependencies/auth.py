from fastapi import HTTPException, Request, status

from app.core.config import get_settings


def require_admin(request: Request):
    settings = get_settings()

    # Temporary local-dev shortcut so the app can boot and routes can be tested.
    # We will replace this with Cognito JWT verification next.
    if settings.auth_bypass:
        return {
            "sub": "local-dev-user",
            "email": "dev@example.com",
            "groups": ["admin"],
        }

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Cognito verification not wired yet",
    )