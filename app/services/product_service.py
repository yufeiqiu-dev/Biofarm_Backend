from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.exc import IntegrityError

from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.schemas.product import ProductCreate, ProductUpdate
from app.services.s3_service import delete_s3_objects_by_urls


def list_products(db: Session) -> list[Product]:
    stmt = (
        select(Product)
        .options(selectinload(Product.variants))
        .order_by(Product.name)
    )
    return list(db.scalars(stmt).all())


def list_public_products(
    db: Session,
    search: str | None = None,
    tags: list[str] | None = None,
) -> list[Product]:
    stmt = (
        select(Product)
        .options(selectinload(Product.variants))
        .where(Product.variants.any())
        .order_by(Product.name)
    )
    if search:
        term = f"%{search}%"
        stmt = stmt.where(
            or_(
                Product.name.ilike(term),
                Product.description.ilike(term),
            )
        )
    results = list(db.scalars(stmt).all())
    if tags:
        tag_set = set(tags)
        results = [p for p in results if tag_set.issubset(set(p.tags or []))]
    return results


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
        tags=payload.tags,
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
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError("Product cat_id or variant catalog_id already exists")
    db.refresh(db_product)

    return get_product_by_id(db, db_product.id)


def update_product(db: Session, product_id: UUID, payload: ProductUpdate) -> Product | None:
    db_product = get_product_by_id(db, product_id)
    if db_product is None:
        return None

    # update product scalar fields only
    product_update_data = payload.model_dump(
        exclude_unset=True,
        exclude={"variants"},
        exclude_none=False,
    )

    for field, value in product_update_data.items():
        setattr(db_product, field, value)

    # if variants is included, sync variants to match payload
    if payload.variants is not None:
        existing_variants_by_id = {variant.id: variant for variant in db_product.variants}
        kept_variant_ids = set()

        for variant_payload in payload.variants:
            if variant_payload.id is None:
                # create new variant
                new_variant = ProductVariant(
                    catalog_id=variant_payload.catalog_id,
                    size_value=variant_payload.size_value,
                    size_unit=variant_payload.size_unit,
                    price=variant_payload.price,
                    stock=variant_payload.stock,
                )
                db_product.variants.append(new_variant)
            else:
                # update existing variant
                db_variant = existing_variants_by_id.get(variant_payload.id)
                if db_variant is None:
                    raise ValueError(f"Variant {variant_payload.id} does not belong to product {product_id}")

                db_variant.catalog_id = variant_payload.catalog_id
                db_variant.size_value = variant_payload.size_value
                db_variant.size_unit = variant_payload.size_unit
                db_variant.price = variant_payload.price
                db_variant.stock = variant_payload.stock

                kept_variant_ids.add(db_variant.id)

        # delete old variants not included in payload
        for db_variant in list(db_product.variants):
            if db_variant.id is not None and db_variant.id not in kept_variant_ids:
                # only delete existing DB variants that were omitted
                # newly-added variants won't be in kept_variant_ids yet, so skip those by checking if they were in existing_variants_by_id
                if db_variant.id in existing_variants_by_id:
                    db.delete(db_variant)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise
    return get_product_by_id(db, product_id)


def delete_product(db: Session, product_id: UUID) -> bool:
    db_product = get_product_by_id(db, product_id)
    if db_product is None:
        return False

    image_urls = list(db_product.image_urls or [])

    db.delete(db_product)
    db.commit()

    if image_urls:
        try:
            delete_s3_objects_by_urls(image_urls)
        except RuntimeError:
            pass  # S3 cleanup is best-effort; product is already deleted from DB

    return True