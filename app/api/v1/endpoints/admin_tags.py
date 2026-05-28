from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies.auth import require_admin
from app.schemas.tag import TagCreate, TagOut
from app.services.tag_service import create_tag, delete_tag, list_tags

router = APIRouter(prefix="/admin/tags", tags=["admin-tags"])


@router.get("", response_model=list[TagOut])
def get_tags(db: Session = Depends(get_db), _=Depends(require_admin)):
    return list_tags(db)


@router.post("", response_model=TagOut, status_code=status.HTTP_201_CREATED)
def add_tag(payload: TagCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    try:
        return create_tag(db, payload)
    except IntegrityError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tag already exists")


@router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_tag(tag_id: UUID, db: Session = Depends(get_db), _=Depends(require_admin)):
    if not delete_tag(db, tag_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
