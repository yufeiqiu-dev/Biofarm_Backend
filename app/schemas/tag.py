import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class TagCreate(BaseModel):
    # strip_whitespace runs before the length check. Declared the other way
    # round, "   " passed min_length=1 and the service's own .strip() then wrote
    # an empty tag name that nothing could match or usefully display.
    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ]


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
