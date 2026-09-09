"""End-to-end test script for the CUHK-Shenzhen web2api server.

This script tests all API endpoints using the glm-5.3-flash model.

Usage:
    1. Start the server: python scripts/server.py
    2. Run this script: python scripts/e2e_test.py
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import requests

BASE_URL = "http://127.0.0.1:8765"
DEFAULT_MODEL = "glm-5.3-flash"


def test_health() -> dict[str, Any]:
    """Test /health endpoint."""
    print("=== Testing /health ===")
    try:
        resp = requests.get(f"{BASE_URL}/health", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        print(f"  Status: {resp.status_code}")
        print(f"  Session ID: {data.get('session_id', 'N/A')[:20]}...")
        print(f"  User: {data.get('user', {}).get('username', 'N/A')}")
        return data
    except requests.exceptions.ConnectionError:
        print("  ERROR: Server not running. Start with: python scripts/server.py")
        sys.exit(1)
    except Exception as e:
        print(f"  ERROR: {e}")
        raise


def test_model_info() -> dict[str, Any]:
    """Test GET /model endpoint."""
    print("\n=== Testing GET /model ===")
    resp = requests.get(f"{BASE_URL}/model", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    print(f"  Current model: {data.get('current')}")
    print(f"  Available models: {len(data.get('available', []))} models")
    return data


def test_switch_model(model: str) -> dict[str, Any]:
    """Test POST /model endpoint."""
    print(f"\n=== Testing POST /model (switch to {model}) ===")
    resp = requests.post(
        f"{BASE_URL}/model",
        json={"approach_id": model},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"  Switched to: {data.get('current')}")
    return data


def test_response_non_streaming(message: str, model: str) -> dict[str, Any]:
    """Test POST /response with stream=false."""
    print(f"\n=== Testing POST /response (non-streaming) ===")
    print(f"  Message: {message}")
    print(f"  Model: {model}")
    
    start = time.time()
    resp = requests.post(
        f"{BASE_URL}/response",
        json={
            "message": message,
            "approach_id": model,
            "stream": False,
        },
        timeout=120,
    )
    elapsed = time.time() - start
    
    resp.raise_for_status()
    data = resp.json()
    print(f"  Status: {resp.status_code}")
    print(f"  Response time: {elapsed:.2f}s")
    print(f"  Session ID: {data.get('chat_session_id', 'N/A')[:20]}...")
    print(f"  Reply status: {data.get('status')}")
    print(f"  Text length: {len(data.get('text', ''))} chars")
    print(f"  Tools used: {len(data.get('tools', []))} tools")
    print(f"  Text preview: {data.get('text', '')[:100]}...")
    return data


def test_response_streaming(message: str, model: str) -> dict[str, Any]:
    """Test POST /response with stream=true."""
    print(f"\n=== Testing POST /response (streaming) ===")
    print(f"  Message: {message}")
    print(f"  Model: {model}")
    
    start = time.time()
    resp = requests.post(
        f"{BASE_URL}/response",
        json={
            "message": message,
            "approach_id": model,
            "stream": True,
        },
        stream=True,
        timeout=120,
    )
    resp.raise_for_status()
    
    events = []
    full_text = ""
    for line in resp.iter_lines():
        if line:
            line_str = line.decode("utf-8")
            if line_str.startswith("data: "):
                try:
                    event = json.loads(line_str[6:])
                    events.append(event)
                    if event.get("event") == "msg":
                        item = event.get("item", {})
                        if item.get("type") == "text":
                            full_text += item.get("content", "")
                except json.JSONDecodeError:
                    pass
    
    elapsed = time.time() - start
    print(f"  Status: {resp.status_code}")
    print(f"  Response time: {elapsed:.2f}s")
    print(f"  Total events: {len(events)}")
    print(f"  Text length: {len(full_text)} chars")
    print(f"  Text preview: {full_text[:100]}...")
    return {"events": events, "text": full_text}


def test_chat_legacy(message: str, model: str) -> dict[str, Any]:
    """Test POST /chat (legacy endpoint)."""
    print(f"\n=== Testing POST /chat (legacy) ===")
    print(f"  Message: {message}")
    
    start = time.time()
    resp = requests.post(
        f"{BASE_URL}/chat",
        json={
            "message": message,
            "approach_id": model,
        },
        timeout=120,
    )
    elapsed = time.time() - start
    
    resp.raise_for_status()
    data = resp.json()
    print(f"  Status: {resp.status_code}")
    print(f"  Response time: {elapsed:.2f}s")
    print(f"  Reply status: {data.get('status')}")
    print(f"  Text length: {len(data.get('text', ''))} chars")
    print(f"  Text preview: {data.get('text', '')[:100]}...")
    return data


def test_sessions() -> list[dict[str, Any]]:
    """Test GET /sessions endpoint."""
    print("\n=== Testing GET /sessions ===")
    resp = requests.get(f"{BASE_URL}/sessions", params={"limit": 5}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    print(f"  Sessions found: {len(data)}")
    for i, session in enumerate(data[:3]):
        print(f"  {i+1}. {session.get('title', 'Untitled')[:50]}")
    return data


def test_tools_list() -> list[dict[str, Any]]:
    """Test GET /tools endpoint."""
    print("\n=== Testing GET /tools ===")
    resp = requests.get(f"{BASE_URL}/tools", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    print(f"  Tools registered: {len(data)}")
    for tool in data:
        print(f"  - {tool.get('name')}: {tool.get('description', '')[:50]}")
    return data


def test_tool_call() -> dict[str, Any]:
    """Test POST /tools/call endpoint."""
    print("\n=== Testing POST /tools/call ===")
    resp = requests.post(
        f"{BASE_URL}/tools/call",
        json={
            "tool_name": "web_search",
            "arguments": {"query": "CUHK-Shenzhen"},
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"  Tool call ID: {data.get('tool_call_id')}")
    print(f"  Status: {data.get('status')}")
    print(f"  Result length: {len(data.get('content', ''))} chars")
    return data


def test_tool_log() -> list[dict[str, Any]]:
    """Test GET /tools/log endpoint."""
    print("\n=== Testing GET /tools/log ===")
    resp = requests.get(f"{BASE_URL}/tools/log", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    print(f"  Log entries: {len(data)}")
    return data


def test_response_with_tools(message: str, model: str) -> dict[str, Any]:
    """Test POST /response with tool_proxy enabled."""
    print(f"\n=== Testing POST /response with tool_proxy ===")
    print(f"  Message: {message}")
    print(f"  Model: {model}")
    
    start = time.time()
    resp = requests.post(
        f"{BASE_URL}/response",
        json={
            "message": message,
            "approach_id": model,
            "tool_proxy": True,
            "stream": False,
        },
        timeout=120,
    )
    elapsed = time.time() - start
    
    resp.raise_for_status()
    data = resp.json()
    print(f"  Status: {resp.status_code}")
    print(f"  Response time: {elapsed:.2f}s")
    print(f"  Text length: {len(data.get('text', ''))} chars")
    print(f"  Tools used: {len(data.get('tools', []))} tools")
    if data.get('tools'):
        for tool in data['tools']:
            print(f"    - {tool.get('tool_name')}: {tool.get('status')}")
    print(f"  Text preview: {data.get('text', '')[:100]}...")
    return data


def main():
    """Run all e2e tests."""
    print("=" * 60)
    print("CUHK-Shenzhen web2api E2E Test")
    print("=" * 60)
    
    # Test 1: Health check
    test_health()
    
    # Test 2: Model info
    test_model_info()
    
    # Test 3: Switch model
    test_switch_model(DEFAULT_MODEL)
    
    # Test 4: Non-streaming response
    test_response_non_streaming("你好，请用一句话介绍自己", DEFAULT_MODEL)
    
    # Test 5: Streaming response
    test_response_streaming("请解释什么是人工智能", DEFAULT_MODEL)
    
    # Test 6: Legacy chat endpoint
    test_chat_legacy("1+1等于多少", DEFAULT_MODEL)
    
    # Test 7: Sessions
    test_sessions()
    
    # Test 8: Tools list
    test_tools_list()
    
    # Test 9: Tool call
    test_tool_call()
    
    # Test 10: Tool log
    test_tool_log()
    
    # Test 11: Response with tool proxy
    test_response_with_tools("搜索一下CUHK-Shenzhen的最新新闻", DEFAULT_MODEL)
    
    print("\n" + "=" * 60)
    print("All tests completed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
