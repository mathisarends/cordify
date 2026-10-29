import json
from datetime import date
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field

from harnessed import Agent, Context, Inject, Plugin, ToolCall, tool
from harnessed.core.tools.registry import ToolRegistry
from harnessed.plugins import Service
from harnessed.testing import ScriptedModel


class Priority(StrEnum):
    LOW = "low"
    HIGH = "high"


class Address(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city: str = Field(min_length=2)
    postal_code: str = Field(pattern=r"^[0-9]{5}$")


class Ticket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=60)
    priority: Priority
    address: Address
    labels: list[str] = Field(default_factory=list, max_length=3)


@pytest.mark.parametrize("parameter", ["missing", "service"])
async def test_mount_constraints_reject_unknown_and_injected_parameters(parameter):
    class Tools(Plugin):
        def fields(self):
            return {parameter: Field(le=5)}

        @tool(parameter_fields=fields)
        async def value(self, count: int, *, service: Inject[int]) -> int:
            return count + service

    with pytest.raises(ValueError, match="Unknown model parameters"):
        async with Agent(model=ScriptedModel([]), plugins=[Tools(), Service(int, 1)]).session():
            pytest.fail("Invalid parameter metadata was mounted")


async def test_nested_form_models_are_validated_instantiated_and_serialized_as_json():
    payload = {
        "title": "Fix login",
        "priority": "high",
        "address": {"city": "Berlin", "postal_code": "10115"},
    }
    async with Context() as ctx:
        registry = ToolRegistry(ctx)
        ctx.provide(int, 7)

        @registry.tool
        async def submit(ticket: Ticket, owner: Inject[int]) -> Ticket:
            assert isinstance(ticket, Ticket) and isinstance(ticket.address, Address)
            assert ticket.priority is Priority.HIGH and owner == 7
            return ticket.model_copy(update={"labels": ["validated"]})

        schema = registry.definitions[0].parameters
        assert set(schema["properties"]) == {"ticket"}
        assert schema["$defs"]["Ticket"]["properties"]["title"]["minLength"] == 3
        assert schema["$defs"]["Priority"]["enum"] == ("low", "high")
        result = await registry.execute(ToolCall("submit", {"ticket": payload}))
        assert not result.is_error
        assert json.loads(result.content) == {**payload, "labels": ["validated"]}


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "x"},
        {"priority": "urgent"},
        {"priority": 1},
        {"address": {"city": "B", "postal_code": "bad"}},
        {"address": {"city": "Berlin", "postal_code": 10115}},
        {"labels": ["a", "b", "c", "d"]},
        {"unexpected": True},
    ],
)
async def test_invalid_nested_form_values_never_reach_the_tool(changes):
    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        @registry.tool
        async def submit(ticket: Ticket) -> str:
            pytest.fail("Invalid form reached the tool")

        payload = {
            "title": "Fix login",
            "priority": "high",
            "address": {"city": "Berlin", "postal_code": "10115"},
            **changes,
        }
        result = await registry.execute(ToolCall("submit", {"ticket": payload}))
        assert (
            result.is_error and "ValidationError" in result.content and "ticket" in result.content
        )


type PageSize = Annotated[int, Field(ge=1, le=100)]


@pytest.mark.parametrize("size", [0, 101, "5", True, 2.5])
async def test_pep695_annotated_constraints_use_strict_json_validation(size):
    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        @registry.tool
        async def search(size: PageSize = 10) -> int:
            return size

        assert (await registry.execute(ToolCall("search", {"size": size}))).is_error
        assert (await registry.execute(ToolCall("search"))).content == "10"


async def test_json_native_enum_date_and_uuid_forms_arrive_as_native_python_values():
    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        @registry.tool
        async def schedule(priority: Priority, day: date, reference: UUID) -> dict:
            assert isinstance(priority, Priority) and isinstance(day, date)
            assert isinstance(reference, UUID)
            return {"priority": priority, "day": day, "reference": reference}

        payload = {
            "priority": "low",
            "day": "2026-10-28",
            "reference": "00000000-0000-0000-0000-000000000001",
        }
        result = await registry.execute(ToolCall("schedule", payload))
        assert not result.is_error and json.loads(result.content) == payload


async def test_parameter_names_cannot_collide_with_pydantic_fields_or_internal_aliases():
    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        @registry.tool
        async def names(model_dump: str, _private: int) -> str:
            return f"{model_dump}:{_private}"

        assert set(registry.definitions[0].parameters["properties"]) == {"model_dump", "_private"}
        result = await registry.execute(ToolCall("names", {"model_dump": "ok", "_private": 2}))
        assert result.content == "ok:2"
        assert (await registry.execute(ToolCall("names", {"argument_0": "injected"}))).is_error


async def test_default_factories_and_constraints_are_evaluated_for_each_call():
    async with Context() as ctx:
        registry = ToolRegistry(ctx)
        default_values = Field(default_factory=list)

        @registry.tool
        async def collect(values: list[int] = default_values) -> list[int]:
            values.append(1)
            return values

        assert (await registry.execute(ToolCall("collect"))).content == "[1]"
        assert (await registry.execute(ToolCall("collect"))).content == "[1]"


async def test_nonfinite_results_are_not_silently_serialized_as_null():
    async with Context() as ctx:
        registry = ToolRegistry(ctx)

        @registry.tool
        async def invalid() -> dict:
            return {"number": float("nan")}

        assert (await registry.execute(ToolCall("invalid"))).is_error
