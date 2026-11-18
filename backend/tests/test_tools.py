from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from app.core.tools import Access, PermissionDeniedError, Tool, ToolRegistry, ToolSet


class AddIn(BaseModel):
    a: int = Field(description="first")
    b: int = 0


def add(args: AddIn) -> dict[str, int]:
    return {"sum": args.a + args.b}


async def add_async(args: AddIn) -> int:
    return args.a + args.b


def boom(args: AddIn) -> None:
    raise RuntimeError("kaboom")


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool("add", "Add numbers.", AddIn, add))
    reg.register(Tool("propose_x", "Propose.", AddIn, add, Access.PROPOSE))
    reg.register(Tool("execute_x", "Really write.", AddIn, add, Access.EXECUTE))
    return reg


def test_schema_generated_from_pydantic_without_titles() -> None:
    d = Tool("add", "Add numbers.", AddIn, add).api_definition()
    assert d["name"] == "add"
    schema = d["input_schema"]
    assert schema["properties"]["a"] == {"description": "first", "type": "integer"}
    assert schema["required"] == ["a"]
    assert schema["additionalProperties"] is False
    assert "title" not in json.dumps(schema)


async def test_call_ok_sync_and_async() -> None:
    ts = ToolSet([Tool("add", "d", AddIn, add), Tool("add2", "d", AddIn, add_async)])
    assert json.loads((await ts.call("add", {"a": 1, "b": 2})).content) == {"sum": 3}
    assert (await ts.call("add2", {"a": 4, "b": 5})).content == "9"


async def test_invalid_input_is_structured_error() -> None:
    ts = ToolSet([Tool("add", "d", AddIn, add)])
    out = await ts.call("add", {"a": "not-a-number"})
    assert out.is_error and out.error_type == "invalid_input"
    assert "a" in json.loads(out.content)["error"]["message"]


async def test_tool_exception_is_structured_error() -> None:
    ts = ToolSet([Tool("boom", "d", AddIn, boom)])
    out = await ts.call("boom", {"a": 1})
    assert out.is_error and out.error_type == "tool_failed"
    assert "kaboom" in out.content


async def test_unknown_tool_rejected() -> None:
    out = await ToolSet([Tool("add", "d", AddIn, add)]).call("delete_everything", {})
    assert out.is_error and out.error_type == "unknown_tool"


def test_execute_tools_can_never_be_granted() -> None:
    reg = make_registry()
    with pytest.raises(PermissionDeniedError):
        reg.grant("calendar", ["execute_x"])
    with pytest.raises(PermissionDeniedError):
        ToolSet([Tool("execute_x", "d", AddIn, add, Access.EXECUTE)])


async def test_agent_only_gets_granted_tools() -> None:
    reg = make_registry()
    reg.grant("calendar", ["add", "propose_x"])
    ts = reg.toolset_for("calendar")
    assert ts.names == ["add", "propose_x"]
    # An ungranted-but-registered tool is unreachable even if the model asks for it by name.
    out = await ts.call("execute_x", {"a": 1})
    assert out.error_type == "unknown_tool"


def test_unknown_agent_and_unknown_tool_grants() -> None:
    reg = make_registry()
    with pytest.raises(PermissionDeniedError):
        reg.toolset_for("nobody")
    with pytest.raises(KeyError):
        reg.grant("a", ["nope"])
    with pytest.raises(ValueError):
        reg.register(Tool("add", "dup", AddIn, add))


def test_permissions_table() -> None:
    reg = make_registry()
    reg.grant("calendar", ["add", "propose_x"])
    assert reg.permissions() == {"calendar": [("add", Access.READ), ("propose_x", Access.PROPOSE)]}
