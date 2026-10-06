import os
import sys
import json

# Add current dir to sys.path
sys.path.insert(0, r"D:\Projects\unstream-proxy")

import Proxy

print("Testing Proxy.py functions...")

# Test 1: extract_tag_name
print("\n--- Test 1: extract_tag_name ---")
test_attrs = [
    ('name="mcp__ghidra__check_tools"', "mcp__ghidra__check_tools"),
    ("name='mcp__ghidra__check_tools'", "mcp__ghidra__check_tools"),
    ('tool="mcp__ghidra__check_tools"', "mcp__ghidra__check_tools"),
    ('function="mcp__ghidra__check_tools"', "mcp__ghidra__check_tools"),
    ('name=mcp__ghidra__check_tools', "mcp__ghidra__check_tools"),
    ('name="Bash" timeout="10"', "Bash"),
]
for attr_str, expected in test_attrs:
    res = Proxy.extract_tag_name(attr_str)
    print(f"{attr_str!r} -> {res!r}")
    assert res == expected, f"Expected {expected}, got {res}"
print("Test 1 passed!")

# Test 2: parse_parameters
print("\n--- Test 2: parse_parameters ---")
param_text = '<parameter name="command">ls -la</parameter><parameter name="flag">true</parameter>'
params = Proxy.parse_parameters(param_text)
print("Parsed params:", params)
assert params == {"command": "ls -la", "flag": True}
print("Test 2 passed!")

# Test 3: parse_tool_call with parameterless call
print("\n--- Test 3: parse_tool_call parameterless ---")
call_empty = Proxy.parse_tool_call("", opener_name="mcp__ghidra__check_tools")
assert call_empty == ("mcp__ghidra__check_tools", {}), f"Failed: {call_empty}"
print("Empty body ->", call_empty)

call_spaces = Proxy.parse_tool_call("   \n\t  ", opener_name="mcp__ghidra__list_instances")
assert call_spaces == ("mcp__ghidra__list_instances", {}), f"Failed: {call_spaces}"
print("Whitespace body ->", call_spaces)

call_empty_json = Proxy.parse_tool_call("{}", opener_name="mcp__ghidra__check_tools")
assert call_empty_json == ("mcp__ghidra__check_tools", {}), f"Failed: {call_empty_json}"
print("{} body ->", call_empty_json)
print("Test 3 passed!")

# Test 4: split_emulated_calls and extract_tool_calls with self-closing and empty tags
print("\n--- Test 4: split_emulated_calls & extract_tool_calls ---")
resp_test = {
    "role": "assistant",
    "stop_reason": "end_turn",
    "content": [
        {
            "type": "text",
            "text": "Found `meow.dll`. Now let me find the ghidra MCP server.\n<invoke name=\"mcp__ghidra__check_tools\"></invoke>"
        }
    ]
}

extracted = Proxy.extract_tool_calls(dict(resp_test))
print("Extracted blocks count:", len(extracted["content"]))
for b in extracted["content"]:
    print("Block:", b)
assert extracted["stop_reason"] == "tool_use"
assert any(b.get("type") == "tool_use" and b.get("name") == "mcp__ghidra__check_tools" for b in extracted["content"])
print("Test 4 passed!")

# Test 5: Self-closing tag
print("\n--- Test 5: Self-closing tag ---")
resp_test_self = {
    "role": "assistant",
    "stop_reason": "end_turn",
    "content": [
        {
            "type": "text",
            "text": "Checking tools: <invoke name=\"mcp__ghidra__list_instances\"/> Done!"
        }
    ]
}
extracted_self = Proxy.extract_tool_calls(dict(resp_test_self))
print("Extracted blocks count:", len(extracted_self["content"]))
for b in extracted_self["content"]:
    print("Block:", b)
assert extracted_self["stop_reason"] == "tool_use"
assert any(b.get("type") == "tool_use" and b.get("name") == "mcp__ghidra__list_instances" for b in extracted_self["content"])
print("Test 5 passed!")

# Test 6: In-band tool call with parameters
print("\n--- Test 6: In-band tool call with parameters ---")
resp_with_param = {
    "role": "assistant",
    "stop_reason": "end_turn",
    "content": [
        {
            "type": "text",
            "text": 'Let me run a command:\n<invoke name="Bash"><parameter name="command">whoami</parameter></invoke>'
        }
    ]
}
extracted_param = Proxy.extract_tool_calls(dict(resp_with_param))
assert extracted_param["stop_reason"] == "tool_use"
tool_block = next(b for b in extracted_param["content"] if b.get("type") == "tool_use")
assert tool_block["name"] == "Bash"
assert tool_block["input"] == {"command": "whoami"}
print("Extracted param tool block:", tool_block)
print("Test 6 passed!")

# Test 7: Safety guard when no tool calls are found
print("\n--- Test 7: Safety guard when no tools found ---")
resp_no_tool = {
    "role": "assistant",
    "stop_reason": "tool_use", # relay returned tool_use erroneously without blocks
    "content": [
        {"type": "text", "text": "Just regular text."}
    ]
}
extracted_no_tool = Proxy.extract_tool_calls(dict(resp_no_tool))
assert extracted_no_tool["stop_reason"] == "end_turn"
print("Safety fallback stop_reason:", extracted_no_tool["stop_reason"])
print("Test 7 passed!")

# Test 8: SSE streaming generation with tool_use block
print("\n--- Test 8: SSE streaming generation ---")
sse_chunks = list(Proxy.generate_sse_stream(extracted))
sse_output = "".join(sse_chunks)
assert "event: content_block_start" in sse_output
assert '"type": "tool_use"' in sse_output
assert '"name": "mcp__ghidra__check_tools"' in sse_output
assert '"stop_reason": "tool_use"' in sse_output
print("SSE generation verified successfully! Output sample:")
print(sse_output[:300] + "...")
print("Test 8 passed!")

print("\n==========================================")
print("ALL PROXY TESTS PASSED WITH 100% SUCCESS!")
print("==========================================")
