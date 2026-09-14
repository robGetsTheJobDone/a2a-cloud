from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ConsumerSetupKind = Literal["config", "secret"]
ConsumerSetupInputType = Literal[
    "text",
    "password",
    "url",
    "email",
    "textarea",
    "number",
    "boolean",
    "select",
]

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class ConsumerSetupField(BaseModel):
    """One caller-provided value required or accepted by an agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: ConsumerSetupKind = "config"
    label: str | None = None
    description: str = ""
    required: bool = True
    input_type: ConsumerSetupInputType = "text"
    options: tuple[str, ...] = ()

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        clean = value.strip()
        if not _NAME_RE.fullmatch(clean):
            raise ValueError(
                "consumer setup field names must use environment variable syntax"
            )
        return clean

    @field_validator("label")
    @classmethod
    def _clean_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip()
        return clean or None

    @classmethod
    def config(
        cls,
        name: str,
        *,
        label: str | None = None,
        description: str = "",
        required: bool = True,
        input_type: ConsumerSetupInputType = "text",
        options: tuple[str, ...] = (),
    ) -> "ConsumerSetupField":
        return cls(
            name=name,
            kind="config",
            label=label,
            description=description,
            required=required,
            input_type=input_type,
            options=options,
        )

    @classmethod
    def secret(
        cls,
        name: str,
        *,
        label: str | None = None,
        description: str = "",
        required: bool = True,
        input_type: ConsumerSetupInputType = "password",
    ) -> "ConsumerSetupField":
        return cls(
            name=name,
            kind="secret",
            label=label,
            description=description,
            required=required,
            input_type=input_type,
        )


class ConsumerSetup(BaseModel):
    """Caller-facing setup contract published on the Agent Card.

    Values are supplied by each caller or organization before invocation.
    The card only carries metadata; configured values are never serialized
    into the card.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fields: tuple[ConsumerSetupField, ...] = Field(default_factory=tuple)

    @classmethod
    def none(cls) -> "ConsumerSetup":
        return cls()

    @classmethod
    def from_fields(cls, *fields: ConsumerSetupField) -> "ConsumerSetup":
        return cls(fields=fields)

    @field_validator("fields")
    @classmethod
    def _unique_names(
        cls, fields: tuple[ConsumerSetupField, ...]
    ) -> tuple[ConsumerSetupField, ...]:
        names: set[str] = set()
        for field in fields:
            if field.name in names:
                raise ValueError(f"duplicate consumer setup field: {field.name}")
            names.add(field.name)
        return fields

    @property
    def required_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields if field.required)

