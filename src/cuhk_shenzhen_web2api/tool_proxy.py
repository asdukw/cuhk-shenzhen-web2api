"""Tool proxy for integrating external agent tools (e.g., OpenAI Codex).

This module provides:
- Tool definition models (OpenAI function calling format)
- Tool registry for dynamic tool registration
- Tool call interception and forwarding
- Tool result handling

The proxy intercepts tool calls from the CUHK AI platform and forwards them
to registered external tools, enabling integration with Codex and similar
agent tools.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

# ---- Tool Definition Models (OpenAI function calling format) ----


class ToolParameterProperty(BaseModel):
    """A single property in tool parameters schema."""

    type: str
    description: str | None = None
    enum: list[str] | None = None
    items: dict[str, Any] | None = None


class ToolParameters(BaseModel):
    """Tool parameters schema (JSON Schema format)."""

    type: str = "object"
    properties: dict[str, ToolParameterProperty] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)


class ToolDefinition(BaseModel):
    """Tool definition in OpenAI function calling format."""

    name: str
    description: str
    parameters: ToolParameters = Field(default_factory=ToolParameters)


class ToolCall(BaseModel):
    """A tool call from the AI platform."""

    id: str
    type: str = "function"
    function: dict[str, Any]


class ToolResult(BaseModel):
    """Result of a tool execution."""

    tool_call_id: str
    role: str = "tool"
    content: str


# ---- Tool Handler Protocol ----


class ToolHandler(ABC):
    """Abstract base class for tool handlers."""

    @abstractmethod
    def execute(self, arguments: dict[str, Any]) -> str:
        """Execute the tool with the given arguments.

        Args:
            arguments: Parsed JSON arguments from the tool call.

        Returns:
            Tool execution result as a string.
        """
        ...

    @abstractmethod
    def get_definition(self) -> ToolDefinition:
        """Get the tool definition for registration.

        Returns:
            ToolDefinition describing this tool.
        """
        ...


# ---- Tool Registry ----


class ToolRegistry:
    """Registry for managing tool handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, name: str, handler: ToolHandler) -> None:
        """Register a tool handler.

        Args:
            name: Tool name (must match the name in tool definition).
            handler: ToolHandler implementation.

        Raises:
            ValueError: If name is already registered.
        """
        if name in self._handlers:
            raise ValueError(f"Tool '{name}' is already registered")
        self._handlers[name] = handler
        self._definitions[name] = handler.get_definition()

    def register_function(
        self,
        name: str,
        description: str,
        func: Callable[[dict[str, Any]], str],
        parameters: ToolParameters | None = None,
    ) -> None:
        """Register a simple function as a tool.

        Args:
            name: Tool name.
            description: Tool description.
            func: Function to execute (takes dict, returns str).
            parameters: Optional parameter schema (auto-generated if None).
        """
        if parameters is None:
            parameters = ToolParameters()

        definition = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
        )

        class FunctionToolHandler(ToolHandler):
            def execute(self, arguments: dict[str, Any]) -> str:
                return func(arguments)

            def get_definition(self) -> ToolDefinition:
                return definition

        self.register(name, FunctionToolHandler())

    def unregister(self, name: str) -> None:
        """Unregister a tool.

        Args:
            name: Tool name to unregister.

        Raises:
            KeyError: If tool is not registered.
        """
        if name not in self._handlers:
            raise KeyError(f"Tool '{name}' is not registered")
        del self._handlers[name]
        del self._definitions[name]

    def get_handler(self, name: str) -> ToolHandler | None:
        """Get a tool handler by name.

        Args:
            name: Tool name.

        Returns:
            ToolHandler if registered, None otherwise.
        """
        return self._handlers.get(name)

    def get_definitions(self) -> list[ToolDefinition]:
        """Get all registered tool definitions.

        Returns:
            List of ToolDefinition objects.
        """
        return list(self._definitions.values())

    def get_definition(self, name: str) -> ToolDefinition | None:
        """Get a tool definition by name.

        Args:
            name: Tool name.

        Returns:
            ToolDefinition if registered, None otherwise.
        """
        return self._definitions.get(name)

    def has_tool(self, name: str) -> bool:
        """Check if a tool is registered.

        Args:
            name: Tool name.

        Returns:
            True if tool is registered.
        """
        return name in self._handlers

    def list_tools(self) -> list[str]:
        """List all registered tool names.

        Returns:
            List of tool names.
        """
        return list(self._handlers.keys())


# ---- Tool Proxy ----


class ToolProxy:
    """Proxy for intercepting and executing tool calls.

    This class intercepts tool calls from the CUHK AI platform,
    routes them to registered handlers, and returns results.
    """

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ToolRegistry()
        self._execution_log: list[dict[str, Any]] = []

    def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        """Execute a tool call and return the result.

        Args:
            tool_call: ToolCall to execute.

        Returns:
            ToolResult with execution result.

        Raises:
            ValueError: If tool is not registered.
            RuntimeError: If tool execution fails.
        """
        handler = self.registry.get_handler(tool_call.function.get("name", ""))
        if handler is None:
            raise ValueError(
                f"Tool '{tool_call.function.get('name')}' is not registered"
            )

        try:
            arguments = json.loads(tool_call.function.get("arguments", "{}"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid tool arguments JSON: {e}") from e

        try:
            result = handler.execute(arguments)
        except (ValueError, RuntimeError, KeyError) as e:
            result = f"Tool execution failed: {e}"

        # Log execution
        self._execution_log.append(
            {
                "tool_call_id": tool_call.id,
                "tool_name": tool_call.function.get("name"),
                "arguments": arguments,
                "result": result,
            }
        )

        return ToolResult(
            tool_call_id=tool_call.id,
            content=result,
        )

    def execute_tool_calls(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """Execute multiple tool calls.

        Args:
            tool_calls: List of ToolCall objects.

        Returns:
            List of ToolResult objects.
        """
        results = []
        for tc in tool_calls:
            try:
                result = self.execute_tool_call(tc)
                results.append(result)
            except (ValueError, RuntimeError, KeyError) as e:
                results.append(
                    ToolResult(
                        tool_call_id=tc.id,
                        content=f"Error: {e}",
                    )
                )
        return results

    def get_execution_log(self) -> list[dict[str, Any]]:
        """Get the tool execution log.

        Returns:
            List of execution log entries.
        """
        return self._execution_log.copy()

    def clear_execution_log(self) -> None:
        """Clear the tool execution log."""
        self._execution_log.clear()


# ---- Built-in Tool Examples ----


class WebSearchHandler(ToolHandler):
    """Example web search tool handler (for demonstration)."""

    def execute(self, arguments: dict[str, Any]) -> str:
        """Execute web search (placeholder implementation)."""
        query = arguments.get("query", "")
        # In a real implementation, this would call a search API
        return f"Search results for '{query}': [placeholder results]"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="web_search",
            description="Search the web for information",
            parameters=ToolParameters(
                properties={
                    "query": ToolParameterProperty(
                        type="string",
                        description="Search query",
                    )
                },
                required=["query"],
            ),
        )


class CodexExecuteHandler(ToolHandler):
    """Example Codex code execution handler (for demonstration)."""

    def execute(self, arguments: dict[str, Any]) -> str:
        """Execute code using Codex (placeholder implementation)."""
        code = arguments.get("code", "")
        language = arguments.get("language", "python")
        # In a real implementation, this would call Codex API
        return f"Executed {language} code: {code[:100]}... [placeholder result]"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="codex_execute",
            description="Execute code using OpenAI Codex",
            parameters=ToolParameters(
                properties={
                    "code": ToolParameterProperty(
                        type="string",
                        description="The code to execute",
                    ),
                    "language": ToolParameterProperty(
                        type="string",
                        description="Programming language",
                        enum=["python", "javascript", "typescript"],
                    ),
                },
                required=["code", "language"],
            ),
        )


def create_default_registry() -> ToolRegistry:
    """Create a tool registry with default tools.

    Returns:
        ToolRegistry with default tools registered.
    """
    registry = ToolRegistry()
    registry.register("web_search", WebSearchHandler())
    registry.register("codex_execute", CodexExecuteHandler())
    return registry


# ---- Convenience Functions ----

_default_proxy: ToolProxy | None = None


def get_default_proxy() -> ToolProxy:
    """Get or create the default tool proxy.

    Returns:
        Default ToolProxy instance.
    """
    global _default_proxy
    if _default_proxy is None:
        _default_proxy = ToolProxy(create_default_registry())
    return _default_proxy


def register_tool(
    name: str,
    description: str,
    func: Callable[[dict[str, Any]], str],
    parameters: ToolParameters | None = None,
) -> None:
    """Register a tool with the default proxy.

    Args:
        name: Tool name.
        description: Tool description.
        func: Function to execute.
        parameters: Optional parameter schema.
    """
    get_default_proxy().registry.register_function(name, description, func, parameters)


def execute_tool(tool_name: str, arguments: dict[str, Any]) -> str:
    """Execute a tool with the default proxy.

    Args:
        tool_name: Name of the tool to execute.
        arguments: Tool arguments.

    Returns:
        Tool execution result.

    Raises:
        ValueError: If tool is not registered.
    """
    proxy = get_default_proxy()
    tool_call = ToolCall(
        id=f"call_{hash(tool_name) % 10000}",
        function={"name": tool_name, "arguments": json.dumps(arguments)},
    )
    result = proxy.execute_tool_call(tool_call)
    return result.content
