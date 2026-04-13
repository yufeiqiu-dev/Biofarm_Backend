from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass

from app.models.product import Product
from app.models.product_variant import ProductVariant