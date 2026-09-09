"""What the customer pays to have an order sent.

**The seam, not the rate.** The amount is the only part of shipping that depends
on which carrier is used; everything else - a line on the order, an amount in
the PaymentIntent, a row at checkout and in the confirmation email - is the same
whatever that answer turns out to be. So this function exists now, returning a
configured flat rate, and a real carrier quote replaces its body later without
touching checkout, orders, or email.

Deliberately not in stripe_service: shipping is not Stripe's, even though the
number ends up in a PaymentIntent. It mirrors calculate_tax's shape because that
is the seam a rate provider slots into.

A flat rate rather than live quotes is also the honest choice for now. Live
carrier rates need a weight and box dimensions on every variant, neither of
which is stored - that is data entry for the whole catalogue before a single
quote can be asked for, and it buys little for a catalogue of small cold
packages going to US labs.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.schemas.order import CartItemIn


def calculate_shipping(cart: list[CartItemIn]) -> int:
    """Shipping for this cart, in cents.

    Takes the cart because a real rate will need it - weight is a function of
    what is in the box - even though a flat rate ignores it. Having the argument
    from the start means the call sites do not change when the body does.

    An empty cart ships nothing, so no order can be charged postage for nothing.
    """
    if not cart:
        return 0
    return max(0, get_settings().shipping_flat_cents)
