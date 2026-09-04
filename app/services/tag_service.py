from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tag import Tag
from app.schemas.tag import TagCreate


def list_tags(db: Session) -> list[Tag]:
    return list(db.scalars(select(Tag).order_by(Tag.name)).all())


def get_tag_by_id(db: Session, tag_id: UUID) -> Tag | None:
    return db.scalar(select(Tag).where(Tag.id == tag_id))


def create_tag(db: Session, payload: TagCreate) -> Tag:
    tag = Tag(name=payload.name)
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


def delete_tag(db: Session, tag_id: UUID) -> bool:
    tag = get_tag_by_id(db, tag_id)
    if tag is None:
        return False
    db.delete(tag)
    db.commit()
    return True
