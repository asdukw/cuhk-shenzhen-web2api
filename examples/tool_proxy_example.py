"""Example: Using the Tool Proxy to register and call tools.

This example demonstrates how to:
1. Register a custom tool
2. Call the tool via the proxy
3. Use the tool proxy with the chat API
"""

from cuhk_shenzhen_web2api.tool_proxy import (
    ToolParameters,
    ToolParameterProperty,
    get_default_proxy,
    register_tool,
)


def example_calculator(arguments: dict) -> str:
    """Simple calculator tool for demonstration."""
    operation = arguments.get("operation", "add")
    a = arguments.get("a", 0)
    b = arguments.get("b", 0)

    if operation == "add":
        result = a + b
    elif operation == "subtract":
        result = a - b
    elif operation == "multiply":
        result = a * b
    elif operation == "divide":
        if b == 0:
            return "Error: Division by zero"
        result = a / b
    else:
        return f"Unknown operation: {operation}"

    return str(result)


def main():
    # Example 1: Register a calculator tool
    print("=== Registering Calculator Tool ===")
    register_tool(
        name="calculator",
        description="Perform basic arithmetic operations",
        func=example_calculator,
        parameters=ToolParameters(
            properties={
                "operation": ToolParameterProperty(
                    type="string",
                    description="Arithmetic operation to perform",
                    enum=["add", "subtract", "multiply", "divide"],
                ),
                "a": ToolParameterProperty(
                    type="number",
                    description="First operand",
                ),
                "b": ToolParameterProperty(
                    type="number",
                    description="Second operand",
                ),
            },
            required=["operation", "a", "b"],
        ),
    )
    print("Calculator tool registered successfully!")

    # Example 2: List all registered tools
    print("\n=== Registered Tools ===")
    proxy = get_default_proxy()
    tools = proxy.registry.list_tools()
    print(f"Available tools: {tools}")

    # Example 3: Get tool definitions
    print("\n=== Tool Definitions ===")
    definitions = proxy.registry.get_definitions()
    for defn in definitions:
        print(f"- {defn.name}: {defn.description}")

    # Example 4: Call the calculator tool
    print("\n=== Calling Calculator Tool ===")
    from cuhk_shenzhen_web2api.tool_proxy import ToolCall

    # Call 1: Addition
    tool_call = ToolCall(
        id="call_1",
        function={
            "name": "calculator",
            "arguments": '{"operation": "add", "a": 10, "b": 5}',
        },
    )
    result = proxy.execute_tool_call(tool_call)
    print(f"10 + 5 = {result.content}")

    # Call 2: Division
    tool_call = ToolCall(
        id="call_2",
        function={
            "name": "calculator",
            "arguments": '{"operation": "divide", "a": 20, "b": 4}',
        },
    )
    result = proxy.execute_tool_call(tool_call)
    print(f"20 / 4 = {result.content}")

    # Example 5: View execution log
    print("\n=== Execution Log ===")
    log = proxy.get_execution_log()
    for entry in log:
        print(f"Tool: {entry['tool_name']}, Args: {entry['arguments']}, Result: {entry['result']}")

    # Example 6: Use with response API (conceptual)
    print("\n=== Response API Integration ===")
    print("To use with the response API:")
    print("1. Start the server: python scripts/server.py")
    print("2. Send a message with tool_proxy enabled:")
    print('   curl -X POST http://127.0.0.1:8765/response \\')
    print('     -H "Content-Type: application/json" \\')
    print('     -d \'{"message": "Calculate 15 * 3", "tool_proxy": true}\'')
    print("3. The server will intercept tool calls and execute them")
    print("")
    print("For streaming response:")
    print('   curl -X POST http://127.0.0.1:8765/response \\')
    print('     -H "Content-Type: application/json" \\')
    print('     -d \'{"message": "Calculate 15 * 3", "tool_proxy": true, "stream": true}\'')


if __name__ == "__main__":
    main()
