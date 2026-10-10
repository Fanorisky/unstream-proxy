import os
import sys
import json

# Add current dir to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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

# Test 9: Connection error suppression in _ThreadingProxyServer
print("\n--- Test 9: _ThreadingProxyServer error suppression ---")
super_called = []
orig_handle_error = Proxy.ThreadingHTTPServer.handle_error
def mock_parent_handle_error(self, req, addr):
    super_called.append((req, addr))

Proxy.ThreadingHTTPServer.handle_error = mock_parent_handle_error
try:
    server = Proxy._ThreadingProxyServer.__new__(Proxy._ThreadingProxyServer)

    # 1. ConnectionResetError (10054) - should be suppressed
    try:
        raise ConnectionResetError("[WinError 10054] An existing connection was forcibly closed")
    except ConnectionResetError:
        server.handle_error(None, ("127.0.0.1", 62727))
    assert len(super_called) == 0, f"ConnectionResetError should be suppressed, but parent called: {super_called}"

    # 2. ConnectionAbortedError (10053) - should be suppressed
    try:
        raise ConnectionAbortedError("[WinError 10053] An established connection was aborted")
    except ConnectionAbortedError:
        server.handle_error(None, ("127.0.0.1", 63449))
    assert len(super_called) == 0, f"ConnectionAbortedError should be suppressed, but parent called: {super_called}"

    # 3. Non-connection error - should NOT be suppressed
    try:
        raise RuntimeError("Non-connection error")
    except RuntimeError:
        server.handle_error(None, ("127.0.0.1", 55555))
    assert len(super_called) == 1, "RuntimeError should NOT be suppressed!"
finally:
    Proxy.ThreadingHTTPServer.handle_error = orig_handle_error

print("Connection errors correctly suppressed and other errors forwarded!")
print("Test 9 passed!")

# Test 10: get_json with malformed / non-JSON input
print("\n--- Test 10: get_json defensive parsing ---")
proxy_req = Proxy._RequestProxy()
proxy_req.bind(b"not valid json {{{", {})
assert proxy_req.get_json() is None, "Malformed JSON should return None, not raise!"
proxy_req.bind(b'{"valid": true}', {})
assert proxy_req.get_json() == {"valid": True}
print("Defensive get_json verified!")
print("Test 10 passed!")

# Test 11: passthrough_error wraps Cloudflare HTML into JSON
print("\n--- Test 11: passthrough_error HTML wrapping ---")
cf_html = b"<html><head><title>524 Origin Time-out</title></head><body>error code: 524</body></html>"
mock_upstream = Proxy.UpstreamResponse(524, cf_html, {"content-type": "text/html"})
res_tuple = Proxy.passthrough_error(mock_upstream)
assert isinstance(res_tuple[0], Proxy.Response)
assert res_tuple[1] == 524
res_json = json.loads(res_tuple[0].body.decode("utf-8"))
assert res_json.get("type") == "error"
assert "524" in res_json["error"]["message"]
print("Cloudflare HTML 524 wrapped to JSON:", res_json)
print("Test 11 passed!")

# Test 12: RETRY_POLICY has 429 and 503
print("\n--- Test 12: RETRY_POLICY configuration ---")
assert 429 in Proxy.RETRY_POLICY, "429 should be in RETRY_POLICY"
assert 403 in Proxy.RETRY_POLICY, "403 should be in RETRY_POLICY"
assert 503 in Proxy.RETRY_POLICY, "503 should be in RETRY_POLICY"
print("RETRY_POLICY verified:", Proxy.RETRY_POLICY)
print("Test 12 passed!")

# Test 13: SSE streaming handles string inputs and non-dict content blocks
print("\n--- Test 13: Robust SSE streaming with edge cases ---")
edge_resp = {
    "role": "assistant",
    "content": [
        "raw string block instead of dict",
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": '{"command":"ls"}'}
    ]
}
edge_stream = "".join(list(Proxy.generate_sse_stream(edge_resp)))
assert "raw string block" in edge_stream
assert "Bash" in edge_stream
print("Edge case SSE streaming verified!")
# Test 14: Unparseable non-empty body returns None, not {}
print("\n--- Test 14: Unparseable non-empty body handling ---")
bad_call = Proxy.parse_tool_call("this is completely invalid garbage not json", opener_name="Bash")
assert bad_call is None, f"Expected None for invalid non-empty body, got {bad_call}"
print("Unparseable non-empty body correctly returned None!")
print("Test 14 passed!")

# Test 15: JSON body starting with { and containing <parameter in string
print("\n--- Test 15: JSON body containing <parameter in string ---")
json_with_tag = '{"file": "test.xml", "content": "<parameter name=\\"x\\">val</parameter>"}'
call_with_tag = Proxy.parse_tool_call(json_with_tag, opener_name="Write")
assert call_with_tag is not None
assert call_with_tag[0] == "Write"
assert call_with_tag[1]["file"] == "test.xml"
assert "<parameter" in call_with_tag[1]["content"]
print("JSON body with <parameter string correctly preserved!")
print("Test 15 passed!")

print("\n==========================================")
print("ALL PROXY TESTS PASSED WITH 100% SUCCESS!")
print("==========================================")
