"""Customer-facing order numbers.

These used to be a sequential integer from 1000. That is guessable, and it also
publishes information: anyone who places two orders a month apart can subtract
one number from the other and read off exactly how many orders the business took
in between. Competitors do this. It is the classic reason not to expose a
database counter to customers.

The scheme here is a fixed prefix and eight random characters:

    OB-7K3M9QXZ

Chosen for the situation these actually get used in - read down a phone line,
typed into a support email, copied off a printed invoice:

  - **Crockford's base32 alphabet**, which drops I, L, O and U. Those are the
    characters people mistake for 1, 1, 0 and V, and a customer reading a number
    aloud has no way to disambiguate them.
  - **Uppercase**, so case never has to be communicated.
  - **A hyphen after the prefix**, giving the eye somewhere to break, and making
    an order number recognisable as one when it appears on its own.

Eight characters of a 32-symbol alphabet is 32^8, a little over a trillion
combinations. At any catalogue size this business will plausibly reach, the
chance of a collision is negligible - and `create_order` retries on one anyway,
so a collision costs a re-roll rather than an error.

Not an ordering key. Sort by `created_at`; this says nothing about sequence, and
that is the point.
"""

from __future__ import annotations

import secrets

# Crockford base32: the digits and uppercase letters, without I, L, O or U.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

PREFIX = "OB-"
LENGTH = 8


def generate_order_number() -> str:
    """A new, unguessable, human-readable order number.

    `secrets` rather than `random`: these are shown to customers and used in
    support conversations to identify an order, so being able to predict the next
    one from the last is worth avoiding even though the API checks ownership
    separately.
    """
    return PREFIX + "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


def looks_like_an_order_number(value: str) -> bool:
    """Whether a string has the shape of one. Used for input validation, not to
    decide that a particular order exists."""
    if not value.startswith(PREFIX):
        return False
    body = value[len(PREFIX):]
    return len(body) == LENGTH and all(character in ALPHABET for character in body)
