"""Resolving an order's user_id to the account behind it, for the admin console.

Separate from admin_orders because it talks to Cognito rather than the database,
and because it is the one place in the application that makes a signed Cognito
API call. See app/services/cognito_service.py for what that costs in IAM.
"""

import logging

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies.auth import require_admin
from app.schemas.account import AccountOut
from app.services.cognito_service import get_account

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


@router.get("/{sub}", response_model=AccountOut)
def resolve_account(sub: str, _=Depends(require_admin)):
    """The Cognito account for an order's user_id, or for an email address.

    404 means the account is genuinely gone - deleted from the pool, with its
    orders outliving it, which is a normal thing for an order to do. 502 means
    the lookup could not be made, which is a different fact entirely and must not
    be presented as absence: an unreachable Cognito would otherwise make every
    customer look deleted.
    """
    try:
        account = get_account(sub)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))
    except (BotoCoreError, ClientError) as error:
        logger.exception("Cognito lookup failed for sub %s", sub)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach Cognito: {error}",
        )

    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account with that id",
        )

    return account
