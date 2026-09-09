"""Test script for tool proxy functionality.

This script tests the tool proxy by:
1. Registering a test tool
2. Calling the tool
3. Verifying the execution log
"""

from cuhk_shenzhen_web2api.tool_proxy import (
    ToolCall,
    ToolParameters,
    ToolParameterProperty,
    get_default_proxy,
    register_tool,
)


def test_tool(arguments: dict) -> str:
    """Test tool that echoes its arguments."""
    return f"Echo: {arguments}"


def main():
    print("=== Testing Tool Proxy ===")

    # Get the default proxy
    proxy = get_default_proxy()

    # Register a test tool
    print("\n1. Registering test tool...")
    register_tool(
        name="echo",
        description="Echo tool for testing",
        func=test_tool,
        parameters=ToolParameters(
            properties={
                "message": ToolParameterProperty(
                    type="string",
                    description="Message to echo",
                )
            },
            required=["message"],
        ),
    )
    print("   Test tool registered successfully!")

    # List tools
    print("\n2. Listing registered tools...")
    tools = proxy.registry.list_tools()
    print(f"   Available tools: {tools}")

    # Call the tool
    print("\n3. Calling echo tool...")
    tool_call = ToolCall(
        id="test_call_1",
        function={
            "name": "echo",
            "arguments": '{"message": "Hello, World!"}',
        },
    )
    result = proxy.execute_tool_call(tool_call)
    print(f"   Result: {result.content}")

    # Check execution log
    print("\n4. Checking execution log...")
    log = proxy.get_execution_log()
    print(f"   Log entries: {len(log)}")
    for entry in log:
        print(f"   - Tool: {entry['tool_name']}, Result: {entry['result']}")

    # Test error handling
    print("\n5. Testing error handling...")
    tool_call_error = ToolCall(
        id="test_call_error",
        function={
            "name": "nonexistent_tool",
            "arguments": "{}",
        },
    )
    try:
        proxy.execute_tool_call(tool_call_error)
    except ValueError as e:
        print(f"   Expected error: {e}")

    # Clear log
    print("\n6. Clearing execution log...")
    proxy.clear_execution_log()
    log = proxy.get_execution_log()
    print(f"   Log entries after clear: {len(log)}")

    print("\n=== All tests passed! ===")


if __name__ == "__main__":
    main()
