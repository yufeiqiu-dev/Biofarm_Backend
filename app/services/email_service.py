"""Transactional email, through SES.

Every send goes through here and every one short-circuits on `email_bypass`, the
same shape as stripe_service: local development and the test suite must not put
mail in front of real people, and SES in sandbox refuses unverified recipients
anyway.

**Nothing in this module is allowed to raise.** Two of the three sends happen on
paths where an exception would do real damage:

  - the order confirmation is sent from the Stripe webhook. An exception there
    means a non-200 back to Stripe, Stripe retries, and the retry creates a
    second order for one payment.
  - the shipped notice is sent after the payment has already been captured. A
    failure there must not undo a shipment that has happened.

So a send that fails is logged and swallowed. Mail is worth having and is not
worth an order for.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings
from app.models.order import Order

logger = logging.getLogger(__name__)


@lru_cache
def _ses_client():
    """One client, reused. Creating one per send re-resolves credentials and
    re-opens a connection for every message."""
    settings = get_settings()
    return boto3.client(
        "ses",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key.get_secret_value(),
    )


def _money(amount) -> str:
    return f"${amount:.2f}"


def _order_lines(order: Order) -> str:
    return "\n".join(
        f"  {item.product_name} ({item.variant_label}) x{item.quantity}"
        f"  {_money(item.unit_price * item.quantity)}"
        for item in order.items
    )


def _send(order: Order, subject: str, body: str) -> None:
    """The one place that talks to SES.

    Callers do not check a return value: there is nothing useful for an order
    to do about a bounced send, and making them handle it would put error
    handling for mail on the checkout path.
    """
    settings = get_settings()

    if not order.customer_email:
        # Orders predating the customer_email column carry an empty string. No
        # address is not an error, it is simply nobody to write to.
        logger.info("Order %s has no email address; skipping %r", order.order_number, subject)
        return

    if settings.email_bypass:
        logger.info(
            "EMAIL_BYPASS: not sending %r to %s for order %s",
            subject,
            order.customer_email,
            order.order_number,
        )
        return

    message = {
        "Subject": {"Data": subject},
        "Body": {"Text": {"Data": body}},
    }
    request = {
        "Source": settings.email_from,
        "Destination": {"ToAddresses": [order.customer_email]},
        "Message": message,
    }
    if settings.email_reply_to:
        request["ReplyToAddresses"] = [settings.email_reply_to]

    try:
        _ses_client().send_email(**request)
        logger.info("Sent %r for order %s", subject, order.order_number)
    except (ClientError, BotoCoreError):
        # Logged with the order number so it can be resent by hand. Deliberately
        # not re-raised - see the module docstring.
        logger.exception(
            "Failed to send %r for order %s to %s",
            subject,
            order.order_number,
            order.customer_email,
        )


def send_order_confirmation(order: Order) -> None:
    """Sent when the webhook turns a paid checkout into an order."""
    total = order.total_amount + order.tax_amount
    body = (
        f"Thanks for your order.\n\n"
        f"Order number: {order.order_number}\n\n"
        f"{_order_lines(order)}\n\n"
        f"Subtotal: {_money(order.total_amount)}\n"
        f"Tax:      {_money(order.tax_amount)}\n"
        f"Total:    {_money(total)}\n\n"
        f"Shipping to:\n"
        f"  {order.shipping_name}\n"
        f"  {order.shipping_address1}\n"
        + (f"  {order.shipping_address2}\n" if order.shipping_address2 else "")
        + f"  {order.shipping_city}, {order.shipping_state} {order.shipping_zip}\n\n"
        f"We will email you again when it ships.\n"
    )
    _send(order, f"Order {order.order_number} confirmed", body)


def send_order_shipped(order: Order) -> None:
    """Sent when an admin ships the order, after the payment is captured."""
    tracking = (
        f"Tracking number: {order.tracking_number}\n\n"
        if order.tracking_number
        else "Tracking details will follow separately.\n\n"
    )
    body = (
        f"Your order is on its way.\n\n"
        f"Order number: {order.order_number}\n\n"
        f"{_order_lines(order)}\n\n"
        f"{tracking}"
        f"Shipping to:\n"
        f"  {order.shipping_name}\n"
        f"  {order.shipping_address1}\n"
        f"  {order.shipping_city}, {order.shipping_state} {order.shipping_zip}\n"
    )
    _send(order, f"Order {order.order_number} has shipped", body)


def send_order_cancelled(order: Order) -> None:
    """Sent on cancellation, by either the customer or an admin.

    Says what happened to the money, because that is the only question a
    cancellation email needs to answer: before shipping the authorization is
    voided and nothing was ever taken; afterwards it is refunded.
    """
    body = (
        f"Your order has been cancelled.\n\n"
        f"Order number: {order.order_number}\n\n"
        f"{_order_lines(order)}\n\n"
        f"Any payment for this order has been released. An authorization that "
        f"was never captured simply disappears; a captured payment is refunded "
        f"and can take a few working days to reach your statement.\n\n"
        f"If this was not you, reply to this email.\n"
    )
    _send(order, f"Order {order.order_number} cancelled", body)
