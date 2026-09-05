"""Customer-facing order numbers.

Ten digits, chosen at random:

    4827193056

These used to be a sequential integer from 1000, which is guessable and - the
real problem - publishes information the business would not choose to. Anyone who
places two orders a month apart can subtract one number from the other and read
off exactly how many orders were taken in between.

Random digits keep that fixed while staying purely numeric, which is what
everything downstream of an order number tends to want: accounting software,
spreadsheets, a numeric keypad on a phone, a customer reading it aloud.

Two deliberate choices in the shape:

  - **The first digit is never zero.** Ten digits with a leading zero survives
    the database and the API perfectly well and then loses it the moment someone
    pastes it into a spreadsheet, which is where order numbers spend a lot of
    their life. Avoiding the case entirely is cheaper than explaining it.
  - **Stored as text, not an integer.** Nothing ever does arithmetic on an order
    number, and a numeric column invites exactly that - plus `MAX(order_number)`,
    which is how the sequential version worked and what this replaces.

That leaves 9 x 10^9 possibilities, and it is worth being precise about what that
buys rather than waving at "unlikely". Two different questions:

  - **Will any two orders ever collide?** At 100,000 lifetime orders, about a 43%
    chance - so probably yes, once. That number sounds alarming and is not, for
    the reason below.
  - **Will a given new order collide?** With 100,000 existing orders, 0.001%.

The second is the one that matters, because `create_order` retries on a unique
violation: a collision costs one extra INSERT, not an error and not a duplicate.
Expecting roughly one retry over the lifetime of the business is fine. Widening
to twelve digits would remove even that, at the cost of a number nobody can read
back over a phone.

Not an ordering key. Sort by `created_at`; this says nothing about sequence, and
that is the point.
"""

from __future__ import annotations

import secrets

LENGTH = 10


def generate_order_number() -> str:
    """A new, unguessable order number.

    `secrets` rather than `random`: these are shown to customers and used to
    identify an order in support conversations, so being able to predict the next
    one from the last is worth avoiding - even though every order endpoint checks
    ownership separately, so this is defence in depth rather than the control.
    """
    first = secrets.choice("123456789")
    rest = "".join(secrets.choice("0123456789") for _ in range(LENGTH - 1))
    return first + rest


def looks_like_an_order_number(value: str) -> bool:
    """Whether a string has the shape of one.

    For validating input, not for deciding that a particular order exists.
    """
    return (
        len(value) == LENGTH
        and value.isdigit()
        and value.isascii()  # isdigit() alone accepts Arabic-Indic and other digits
        and not value.startswith("0")
    )
