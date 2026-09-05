import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.order import OrderStatus
from app.schemas.numeric import Money


# --- Request schemas ---

class CartItemIn(BaseModel):
    variant_id: uuid.UUID
    quantity: int = Field(..., ge=1)


class ShippingIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    phone: str = Field(..., min_length=1, max_length=50)
    address1: str = Field(..., min_length=1, max_length=255)
    address2: Optional[str] = Field(default=None, max_length=255)
    city: str = Field(..., min_length=1, max_length=100)
    state: str = Field(..., min_length=2, max_length=2)
    zip: str = Field(..., min_length=1, max_length=20)
    notes: Optional[str] = None


class CreatePaymentIntentRequest(BaseModel):
    cart: list[CartItemIn] = Field(..., min_length=1)
    shipping: ShippingIn


class UpdateOrderStatusRequest(BaseModel):
    status: Literal["confirmed", "shipped", "delivered"]
    tracking_number: Optional[str] = None


class UpdateTrackingRequest(BaseModel):
    tracking_number: str = Field(..., max_length=255)


# --- Response schemas ---

class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    variant_id: Optional[uuid.UUID]
    product_name: str
    variant_label: str
    unit_price: Money
    quantity: int


class AdminOrderItemOut(OrderItemOut):
    current_stock: Optional[int] = None


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_number: int
    status: OrderStatus
    total_amount: Money
    tax_amount: Money = Decimal("0")
    card_brand: str = ""
    card_last4: str = ""
    shipping_name: str
    shipping_phone: str
    shipping_address1: str
    shipping_address2: Optional[str]
    shipping_city: str
    shipping_state: str
    shipping_zip: str
    notes: Optional[str]
    tracking_number: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemOut] = Field(default_factory=list)


class AdminOrderOut(OrderOut):
    user_id: str
    customer_email: str = ""
    stripe_payment_intent_id: str
    items: list[AdminOrderItemOut] = Field(default_factory=list)


class PaymentIntentResponse(BaseModel):
    client_secret: str
    order_id: Optional[uuid.UUID] = None  # only set in bypass mode
    subtotal_cents: int = 0
    tax_amount_cents: int = 0
