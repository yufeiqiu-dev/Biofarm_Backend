"""How decimal quantities cross the API boundary.

Money stays `Decimal` everywhere inside the application - the columns are
`Numeric(10, 2)` and a binary float has no business in a total. Pydantic's
default JSON encoding for `Decimal` is a *string*, though, and that is where
this went wrong: `OrderOut.total_amount` was typed `Decimal` and serialized as
`"19.99"`, while `ProductVariantOut.price` was typed `float` and serialized as
`19.99`. The frontend declared both as `number`, which was false for half of
them, and it only worked because roughly fifteen call sites defensively wrapped
every read in `Number(...)` - with two that did not and survived on JavaScript
coercing a string through `*`.

These aliases keep `Decimal` on the Python side and emit a JSON number, so the
wire format is consistent and the frontend's types are true.

The `max_digits`/`decimal_places` bounds mirror `Numeric(10, 2)`. They matter on
the way in: a price typed `float` let a value with more precision than the
column can hold be bound to it and silently rounded by the database.
"""

from decimal import Decimal
from typing import Annotated

from pydantic import Field, PlainSerializer

_as_number = PlainSerializer(float, return_type=float)

# A monetary amount. Never negative - there are no credits in this schema.
Money = Annotated[
    Decimal,
    Field(max_digits=10, decimal_places=2, ge=0),
    _as_number,
]

# A physical size (500 g, 2.5 mL). Zero is meaningless, so strictly positive.
Measure = Annotated[
    Decimal,
    Field(max_digits=10, decimal_places=2, gt=0),
    _as_number,
]
