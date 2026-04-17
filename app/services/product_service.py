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

    db.commit()
    return get_product_by_id(db, product_id)


def delete_product(db: Session, product_id: UUID) -> bool:
    db_product = get_product_by_id(db, product_id)
    if db_product is None:
        return False

    db.delete(db_product)
    db.commit()
    return True