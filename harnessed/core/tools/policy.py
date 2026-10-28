"""Validated per-tool result policy, independent of plugin implementations."""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import ConfigDict, Field, TypeAdapter


class OutputLimit(Enum):
    """Use the session's default unless a tool explicitly overrides or disables it."""

    INHERIT = "inherit"


type OutputLimitSetting = Annotated[int, Field(ge=256)] | None | OutputLimit
_LIMIT = TypeAdapter(OutputLimitSetting, config=ConfigDict(strict=True, hide_input_in_errors=True))


def validate_output_limit(value: OutputLimitSetting) -> OutputLimitSetting:
    return _LIMIT.validate_python(value)
