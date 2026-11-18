"""Tool definitions, JSON-schema generation and permission-enforcing registry.

Permissions are enforced HERE, in code, not in prompts:

* A ``ToolSet`` is the only thing an ``Agent`` can call. It contains exactly the tools
  granted to that agent; a name outside the set is rejected even if the model invents it.
* ``Access.EXECUTE`` tools (the code paths that really write to external systems, e.g.
  ``execute_approved_action``) can never be granted to any agent.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError


class Access(StrEnum):
    READ = "read"  # reads data only
    PROPOSE = "propose"  # creates a pending action; never touches the external system
    EXECUTE = "execute"  # performs a real write; reachable only from the approval gate


class PermissionDeniedError(Exception):
    pass


# Receives the validated pydantic input model. May be sync or async.
ToolFn = Callable[[Any], Any]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: ToolFn
    access: Access = Access.READ

    def api_definition(self) -> dict[str, Any]:
        """Tool definition (internal format) generated from the pydantic input model."""
        schema = _strip_titles(self.input_model.model_json_schema())
        schema["additionalProperties"] = False
        return {"name": self.name, "description": self.description, "input_schema": schema}


def _strip_titles(node: Any) -> Any:
    """Drop pydantic's auto-generated ``title`` keys (noise for the model, costs tokens)."""
    if isinstance(node, dict):
        return {
            k: _strip_titles(v)
            for k, v in node.items()
            if not (k == "title" and isinstance(v, str))
        }
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    is_error: bool = False
    error_type: str | None = None


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return str(value)


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return json.dumps(value, default=_json_default, ensure_ascii=False)


def _error(error_type: str, message: str) -> ToolOutcome:
    body = json.dumps({"error": {"type": error_type, "message": message}})
    return ToolOutcome(content=body, is_error=True, error_type=error_type)


class ToolSet:
    """The tools one agent may call, plus validated execution."""

    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.access is Access.EXECUTE:
                raise PermissionDeniedError(
                    f"tool {tool.name!r} has EXECUTE access and cannot be given to an agent"
                )
            self._tools[tool.name] = tool

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def definitions(self) -> list[dict[str, Any]]:
        return [t.api_definition() for t in self._tools.values()]

    async def call(self, name: str, raw_input: dict[str, Any]) -> ToolOutcome:
        """Validate input and run the tool. Never raises: failures become structured errors
        the model can read and recover from."""
        tool = self._tools.get(name)
        if tool is None:
            return _error("unknown_tool", f"No tool named {name!r}. Available tools: {self.names}.")
        try:
            args = tool.input_model.model_validate(raw_input)
        except ValidationError as exc:
            issues = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
            )
            return _error("invalid_input", f"Invalid arguments for {name}: {issues}")
        try:
            if inspect.iscoroutinefunction(tool.fn):
                result = await tool.fn(args)
            else:
                result = await asyncio.to_thread(tool.fn, args)
                if inspect.isawaitable(result):
                    result = await result
        except Exception as exc:
            return _error("tool_failed", f"{type(exc).__name__}: {exc}")
        try:
            return ToolOutcome(content=_to_text(result))
        except (TypeError, ValueError) as exc:
            return _error("unserialisable_result", str(exc))


class ToolRegistry:
    """All tools in the app and which agent may use which."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._grants: dict[str, frozenset[str]] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name {tool.name!r}")
        self._tools[tool.name] = tool
        return tool

    def grant(self, agent: str, tool_names: Iterable[str]) -> None:
        names = frozenset(tool_names)
        for name in names:
            tool = self._tools.get(name)
            if tool is None:
                raise KeyError(f"cannot grant unknown tool {name!r} to {agent!r}")
            if tool.access is Access.EXECUTE:
                raise PermissionDeniedError(
                    f"tool {name!r} has EXECUTE access and cannot be granted to {agent!r}"
                )
        self._grants[agent] = self._grants.get(agent, frozenset()) | names

    def permissions(self) -> dict[str, list[tuple[str, Access]]]:
        """Agent -> [(tool, access)], used to generate the permissions table in the docs."""
        return {
            agent: sorted((n, self._tools[n].access) for n in names)
            for agent, names in self._grants.items()
        }

    def describe(self) -> dict[str, list[dict[str, str]]]:
        """Agent -> tools with access level and description, for the Agents page."""
        return {
            agent: [
                {
                    "name": n,
                    "access": self._tools[n].access.value,
                    "description": self._tools[n].description,
                }
                for n in sorted(names)
            ]
            for agent, names in self._grants.items()
        }

    def toolset_for(self, agent: str) -> ToolSet:
        names = self._grants.get(agent)
        if names is None:
            raise PermissionDeniedError(f"agent {agent!r} has no tool grants")
        return ToolSet(self._tools[n] for n in sorted(names))
