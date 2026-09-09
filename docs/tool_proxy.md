# Tool Proxy 工具代理功能

## 概述

工具代理功能允许你将外部agent工具（如OpenAI Codex、自定义函数等）集成到CUHK AI平台中。当启用工具代理时，CUHK AI平台可以调用你注册的外部工具，并将结果返回给用户。

## 架构设计

### 核心组件

1. **ToolDefinition** - 工具定义模型（支持OpenAI function calling格式）
2. **ToolRegistry** - 工具注册表，管理工具处理器
3. **ToolProxy** - 工具代理，拦截和执行工具调用
4. **ToolHandler** - 工具处理器抽象基类

### 工具定义格式

工具定义遵循OpenAI function calling格式：

```python
{
    "name": "calculator",
    "description": "Perform basic arithmetic operations",
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": "Arithmetic operation",
                "enum": ["add", "subtract", "multiply", "divide"]
            },
            "a": {
                "type": "number",
                "description": "First operand"
            },
            "b": {
                "type": "number",
                "description": "Second operand"
            }
        },
        "required": ["operation", "a", "b"]
    }
}
```

## 使用方法

### 1. 注册工具

#### 方法一：使用装饰器

```python
from cuhk_shenzhen_web2api.tool_proxy import register_tool, ToolParameters, ToolParameterProperty

def my_tool(arguments: dict) -> str:
    # 工具逻辑
    return "result"

register_tool(
    name="my_tool",
    description="My custom tool",
    func=my_tool,
    parameters=ToolParameters(
        properties={
            "param1": ToolParameterProperty(
                type="string",
                description="Parameter 1"
            )
        },
        required=["param1"]
    )
)
```

#### 方法二：实现ToolHandler类

```python
from cuhk_shenzhen_web2api.tool_proxy import ToolHandler, ToolDefinition, ToolParameters

class MyToolHandler(ToolHandler):
    def execute(self, arguments: dict) -> str:
        # 工具逻辑
        return "result"
    
    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="my_tool",
            description="My custom tool",
            parameters=ToolParameters(...)
        )

# 注册工具
proxy = get_default_proxy()
proxy.registry.register("my_tool", MyToolHandler())
```

### 2. 调用工具

```python
from cuhk_shenzhen_web2api.tool_proxy import ToolCall, get_default_proxy

proxy = get_default_proxy()

# 创建工具调用
tool_call = ToolCall(
    id="call_1",
    function={
        "name": "my_tool",
        "arguments": '{"param1": "value1"}'
    }
)

# 执行工具调用
result = proxy.execute_tool_call(tool_call)
print(result.content)  # 输出工具结果
```

### 3. 使用API端点

#### 列出所有工具

```bash
curl http://127.0.0.1:8765/tools
```

#### 注册新工具

```bash
curl -X POST http://127.0.0.1:8765/tools \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my_tool",
    "description": "My custom tool",
    "parameters": {
      "param1": {"type": "string", "description": "Parameter 1"}
    }
  }'
```

#### 调用工具

```bash
curl -X POST http://127.0.0.1:8765/tools/call \
  -H "Content-Type: application/json" \
  -d '{
    "tool_name": "my_tool",
    "arguments": {"param1": "value1"}
  }'
```

#### 查看执行日志

```bash
curl http://127.0.0.1:8765/tools/log
```

### 4. 与Chat API集成

#### 使用统一的response端点

```bash
# 非流式响应（默认）
curl -X POST http://127.0.0.1:8765/response \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Calculate 15 * 3",
    "tool_proxy": true
  }'

# 流式响应
curl -X POST http://127.0.0.1:8765/response \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Calculate 15 * 3",
    "tool_proxy": true,
    "stream": true
  }'
```

#### 使用旧的chat端点（兼容）

```bash
# 非流式响应
curl -X POST http://127.0.0.1:8765/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Calculate 15 * 3",
    "tool_proxy": true
  }'

# 流式响应
curl -X POST http://127.0.0.1:8765/chat/stream \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Calculate 15 * 3",
    "tool_proxy": true
  }'
```

#### 使用CLI参数

```bash
# 启用工具代理
python scripts/send_message.py "Calculate 15 * 3" --tool-proxy

# 注册工具
python scripts/send_message.py "test" --register-tool my_tool "My tool" http://localhost:8080/tool
```

## 内置工具

### web_search

- **描述**: 搜索网络信息
- **参数**: `query` (string, required) - 搜索查询

### codex_execute

- **描述**: 使用OpenAI Codex执行代码
- **参数**:
  - `code` (string, required) - 要执行的代码
  - `language` (string, required) - 编程语言 (python/javascript/typescript)

## 错误处理

工具代理提供完善的错误处理机制：

1. **工具未注册**: 当调用未注册的工具时，抛出`ValueError`
2. **参数解析错误**: 当工具参数JSON格式错误时，抛出`RuntimeError`
3. **工具执行错误**: 工具执行失败时，返回错误信息而不是抛出异常

## 执行日志

工具代理会记录所有工具调用：

```python
proxy = get_default_proxy()
log = proxy.get_execution_log()

for entry in log:
    print(f"Tool: {entry['tool_name']}")
    print(f"Arguments: {entry['arguments']}")
    print(f"Result: {entry['result']}")
```

## 最佳实践

1. **工具命名**: 使用清晰、描述性的工具名称
2. **参数验证**: 在工具处理器中验证输入参数
3. **错误处理**: 提供有意义的错误信息
4. **文档**: 为工具提供详细的描述和参数说明
5. **测试**: 在生产环境前充分测试工具

## 示例

查看 `examples/tool_proxy_example.py` 获取完整示例。

## API参考

### ToolProxy类

- `execute_tool_call(tool_call: ToolCall) -> ToolResult` - 执行单个工具调用
- `execute_tool_calls(tool_calls: list[ToolCall]) -> list[ToolResult]` - 执行多个工具调用
- `get_execution_log() -> list[dict]` - 获取执行日志
- `clear_execution_log() -> None` - 清除执行日志

### ToolRegistry类

- `register(name: str, handler: ToolHandler) -> None` - 注册工具处理器
- `register_function(name, description, func, parameters) -> None` - 注册函数作为工具
- `unregister(name: str) -> None` - 注销工具
- `get_handler(name: str) -> ToolHandler | None` - 获取工具处理器
- `get_definitions() -> list[ToolDefinition]` - 获取所有工具定义
- `list_tools() -> list[str]` - 列出所有工具名称

### API端点

- `GET /tools` - 列出所有工具
- `POST /tools` - 注册新工具
- `DELETE /tools/{tool_name}` - 注销工具
- `POST /tools/call` - 调用工具
- `GET /tools/log` - 获取执行日志
- `DELETE /tools/log` - 清除执行日志
