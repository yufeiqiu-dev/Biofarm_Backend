from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.schemas.product import ProductCreate, ProductUpdate


def list_products(db: Session) -> list[Product]:
    stmt = (
        select(Product)
        .options(selectinload(Product.variants))
        .order_by(Product.name)
    )
    return list(db.scalars(stmt).all())


def get_product_by_id(db: Session, product_id: UUID) -> Product | None:
    stmt = (
        select(Product)
        .where(Product.id == product_id)
        .options(selectinload(Product.variants))
    )
    return db.scalar(stmt)


def create_product(db: Session, payload: ProductCreate) -> Product:
    db_product = Product(
        cat_id=payload.cat_id,
        name=payload.name,
        description=payload.description,
        image_url=payload.image_url,
    )

    for variant in payload.variants:
        db_variant = ProductVariant(
            catalog_id=variant.catalog_id,
            size_value=variant.size_value,
            size_unit=variant.size_unit,
            price=variant.price,
            stock=variant.stock,
        )
        db_product.variants.append(db_variant)

    db.add(db_product)
    db.commit()

    return get_product_by_id(db, db_product.id)


def update_product(
    db: Session,
    product_id: UUID,
    payload: ProductUpdate,
) -> Product | None:
    db_product = get_product_by_id(db, product_id)
    if db_product is None:
        return None

    update_data = payload.model_dump(exclude_unset=True)

    for field, value in update_data.items():
        setattr(db_product, field, value)

    db.commit()

    return get_product_by_id(db, product_id)


def delete_product(db: Session, product_id: UUID) -> bool:
    db_product = get_product_by_id(db, product_id)
    if db_product is None:
        return False

    db.delete(db_product)
    db.commit()
    return True