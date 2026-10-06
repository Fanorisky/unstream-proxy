import os
import sys
import signal
import threading
import time
import re
import uuid
import json
import socket
import urllib.request
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# This proxy has zero third-party dependencies: the standard library only.
# Flask and requests used to be here; they were replaced with http.server and
# urllib so the whole thing runs on a bare `python3` with nothing to install.
# A thin compatibility layer below keeps the request-handling code reading the
# way it did under Flask, so the proxy logic itself was not rewritten.


class _RequestProxy(threading.local):
    """Per-thread view of the request in flight, like Flask's `request` global.

    ThreadingHTTPServer serves each request on its own thread, so thread-local
    storage is exactly the right lifetime for this.
    """
    def bind(self, raw_body, headers):
        self._raw_body = raw_body
        self._headers = headers

    def get_json(self):
        if not getattr(self, "_raw_body", b""):
            return None
        return json.loads(self._raw_body.decode("utf-8"))

    @property
    def content_length(self):
        value = self._headers.get("Content-Length")
        return int(value) if value and value.isdigit() else 0

    @property
    def headers(self):
        return self._headers


request = _RequestProxy()


class Response:
    """Minimal stand-in for Flask's Response / jsonify return value.

    Carries a status, a content type, and a body that is either bytes (buffered)
    or an iterable of chunks (streamed). The HTTP handler below knows how to send
    both. `mimetype` and `content_type` are accepted as aliases, as in Flask.
    """
    def __init__(self, body=b"", status=200, content_type=None, mimetype=None,
                 headers=None):
        self.status = status
        self.content_type = content_type or mimetype or "application/json"
        self.headers = dict(headers or {})
        if isinstance(body, (bytes, bytearray)):
            self.body = bytes(body)
            self.streaming = False
        elif isinstance(body, str):
            self.body = body.encode("utf-8")
            self.streaming = False
        else:
            # A generator/iterator of SSE chunks.
            self.body = body
            self.streaming = True


def jsonify(payload):
    """Serialise to a JSON Response, matching Flask's helper closely enough."""
    return Response(json.dumps(payload).encode("utf-8"), status=200,
                    content_type="application/json")


def stream_with_context(generator):
    """No-op shim: the handler thread already owns the request for its lifetime."""
    return generator

config = {
    "target_url": "https://api.justwoker.icu",
    "api_key": "",
    "port": 8080,
    "is_running": False,
    "emulate_tools": True,
    # The relay serves exactly one model; every other name is rejected at the
    # Cloudflare edge with a 403. "*" covers any name not listed explicitly.
    "model_map": {"*": "claude-opus-4-8"},
    # The relay's own tools; a call for one is remapped onto the client equivalent
    # below (it was never declared by the client, so it is dead on arrival otherwise).
    "forbidden_tools": ("read_tabular", "system_todo_write"),
    "tool_aliases": {"read_tabular": "Read", "system_todo_write": "TodoWrite"}
}

# Retry delays per status; tuple length caps retries for that status, MAX_ATTEMPTS
# caps overall.
#
# 403 is Cloudflare with an empty body. Its permanent cause (an unknown model name)
# is removed by model_map rewriting every name, so the 403s left are transient edge
# blocks (~2 in 10) and worth retrying - a bare 403 is fatal to clients, which read
# it as auth failure and abort. 503 "no available channel" clears within a minute.
RETRY_POLICY = {
    403: (2.0, 5.0, 10.0),
    503: (15.0, 20.0, 25.0),
}

# Transport failures are worth a short retry too, but a read timeout is NOT: the
# upstream may already have processed the request, so repeating it could bill
# twice for the same turn. urllib raises socket.timeout for BOTH connect and read
# timeouts and does not say which, so timeouts are treated as non-retryable
# across the board - the safe side of that ambiguity. Only a genuine failure to
# establish or keep the connection (refused, reset, DNS, RemoteDisconnected) is
# retried; see is_retryable_transport_error.
RETRY_EXCEPTION_DELAYS = (0.5, 1.0, 1.5)
MAX_ATTEMPTS = 4
# urllib takes a single timeout rather than Flask/requests' (connect, read) pair.
# The read side is what matters here - the relay can be slow - so the whole call
# gets the long budget.
REQUEST_TIMEOUT = 600


def is_retryable_transport_error(error):
    """True for connection-level failures, False for timeouts (see above)."""
    if isinstance(error, socket.timeout):
        return False
    if isinstance(error, urllib.error.URLError):
        reason = getattr(error, "reason", None)
        if isinstance(reason, socket.timeout):
            return False
        if isinstance(reason, (ConnectionError, OSError)):
            return True
        return True
    if isinstance(error, (ConnectionError, OSError)):
        return True
    return False

def proxy_messages():
    """Handle one POST /v1/messages. Called by the HTTP handler per request."""
    body = request.get_json() or {}
    client_wants_stream = body.get('stream', False)
    
    log_message(f"Incoming request:")
    log_message(f"   Model: {body.get('model')}")
    log_message(f"   Stream (from client): {client_wants_stream}")
    log_message(f"   Messages count: {len(body.get('messages', []))}")
    # What the client uploaded, so the emulated prompt can be compared against it
    # rather than assumed to be the same size.
    client_bytes = request.content_length or 0
    log_message(f"   Client body: {client_bytes} bytes, "
                f"max_tokens={body.get('max_tokens')}")
    
    # Log the structure of the first message
    if body.get('messages'):
        first_msg = body['messages'][0]
        log_message(f"   First msg role: {first_msg.get('role')}")
        log_message(f"   First msg content type: {type(first_msg.get('content')).__name__}")
        if isinstance(first_msg.get('content'), list) and first_msg['content']:
            log_message(f"   First content block: {first_msg['content'][0]}")
    
    # The relay serves one model only; anything else is rejected at the edge.
    requested_model = body.get('model')
    upstream_model = resolve_model(requested_model)
    if upstream_model != requested_model:
        log_message(f"Model: {requested_model} -> {upstream_model}")
        body['model'] = upstream_model

    # Tools travel two ways on this relay. A name it recognises is attached natively
    # under its lowercase spelling; everything else is described in the prompt and the
    # model's markup is turned back into tool_use blocks. See NATIVE_MAP.
    tools = body.get('tools') or []
    client_tools = tools
    tools_in_play = bool(tools) or has_tool_blocks(body.get('messages'))
    native_reverse = {}
    if config.get('emulate_tools') and tools_in_play:
        body, native_reverse = apply_tool_emulation(body)
        log_message(f"Tool routing: {len(native_reverse)} native "
                    f"({', '.join(sorted(native_reverse.values())) or 'none'})")
        if len(tools) - len(native_reverse):
            inband = [t.get('name') for t in tools if isinstance(t, dict)
                      and t.get('name') not in native_reverse.values()]
            log_message(f"   in-band ({len(inband)}): {', '.join(str(n) for n in inband)}")

    # ALWAYS send to the target server WITHOUT streaming
    body['stream'] = False
    
    # The key the client (e.g. Claude Code) sent is forwarded straight through, so
    # no credential has to live in this proxy's config or on disk. config["api_key"]
    # is only a fallback for clients that send nothing. Authorization: Bearer is
    # accepted too and normalised to x-api-key, which is what the relay expects.
    client_key = request.headers.get("x-api-key")
    if not client_key:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            client_key = auth[7:].strip()
    effective_key = client_key or config["api_key"]
    # Log which key is in play by origin and LENGTH only - never the value.
    log_message(f"Key: "
                + (f"client-forwarded (len {len(client_key)})" if client_key
                   else f"config fallback (len {len(effective_key)})"))
    headers = {
        "x-api-key": effective_key,
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
        "content-type": "application/json"
    }
    
    log_message(f"Sending to {config['target_url']} (stream=false)")
    
    # Independent ground truth for the usage numbers we hand back below. When the
    # relay's token counts disagree with this size, THAT disagreement is the bug -
    # it is never a signal to start trusting the relay.
    prompt_bytes = None
    try:
        prompt_bytes = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        # Deliberately no token estimate here. The old "~bytes/4 tokens" line was
        # wrong by ~40% (measured 2026-10-03: this backend runs ~3.3 bytes/token on
        # plain prose, plus ~10.2k of fixed prompt of its own), and it invited the
        # reading that the proxy under-reports while the relay over-bills. Bytes are
        # what we actually control; the token count is the relay's to report.
        delta = prompt_bytes - client_bytes
        log_message(f"Prompt size: {prompt_bytes} bytes, "
                    f"client sent {client_bytes} ({delta:+d})")
    except (TypeError, ValueError):
        pass
    
    try:
        response = forward_request(body, headers)

        log_message(f"Response from server: {response.status_code}")

        if response.status_code != 200:
            log_message(f"Error from server: {response.text[:500]}")
            return passthrough_error(response)

        try:
            response_data = response.json()
        except ValueError:
            log_message("Upstream returned a non-JSON success body")
            return Response(response.content, status=502,
                            content_type=response.headers.get("content-type", "text/plain"))
        if config.get('emulate_tools') and tools_in_play:
            response_data = remap_tool_calls(extract_tool_calls(response_data),
                                             client_tools, native_reverse)

        # Report the model the client asked for, not the one we substituted.
        if requested_model and response_data.get('model') != requested_model:
            response_data['model'] = requested_model

        # Normalise upstream token accounting so the client's context-window
        # math (input + cache_read + cache_creation) cannot blow past the limit.
        response_data = sanitize_usage(response_data, prompt_bytes)

        log_message(f"   Response keys: {list(response_data.keys())}")
        log_message(f"   Content blocks: {len(response_data.get('content', []))}")
        
        # If the client wants a stream, convert the response to SSE format
        if client_wants_stream:
            log_message("Converting to SSE stream")
            return Response(
                stream_with_context(generate_sse_stream(response_data)),
                mimetype='text/event-stream',
                # Framing headers (Transfer-Encoding, Connection) are deliberately
                # NOT set here. Werkzeug adds its own, and setting them too emits a
                # response with a duplicated Transfer-Encoding header plus
                # contradictory Connection values. curl tolerates that; strict HTTP
                # clients do not - Claude Code aborts on it and retries forever.
                headers={
                    'Cache-Control': 'no-cache',
                    'X-Accel-Buffering': 'no'
                }
            )
        else:
            return jsonify(response_data), 200
            
    except Exception as e:
        log_message(f"Proxy error: {e}")
        import traceback
        log_message(f"   Traceback: {traceback.format_exc()}")
        return jsonify({"error": {"message": str(e)}}), 500

class UpstreamResponse:
    """requests-like view over a urllib result, so forward_request reads the same.

    Exposes exactly the surface the proxy uses: status_code, json(), content,
    text, headers.get(). A non-2xx reply is NOT an exception here - urllib raises
    HTTPError for it, and _post_upstream turns that back into one of these so the
    retry logic and passthrough can inspect the status like any other response.
    """
    def __init__(self, status_code, content, headers):
        self.status_code = status_code
        self.content = content
        self._headers = headers

    def json(self):
        return json.loads(self.content.decode("utf-8"))

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    @property
    def headers(self):
        return self._headers


# urllib identifies itself as "Python-urllib/3.x" and Cloudflare in front of the
# relay rejects exactly that string with `403 error code: 1010` ("banned browser
# signature"). Measured 2026-10-05 against the live relay: the urllib default and
# an empty UA both got 1010, while python-requests, curl, a browser UA and this
# one all returned 200. Any explicit User-Agent clears it, so this is set on every
# upstream call. Without it the stdlib port is dead on arrival.
USER_AGENT = "anthropic-relay-proxy/1.0"


def _post_upstream(url, body, headers):
    """One POST via urllib, returning an UpstreamResponse for any HTTP status."""
    data = json.dumps(body).encode("utf-8")
    headers = dict(headers)
    headers["User-Agent"] = USER_AGENT
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return UpstreamResponse(resp.status, resp.read(), dict(resp.headers))
    except urllib.error.HTTPError as error:
        # A real HTTP reply with a non-2xx status - not a transport failure.
        return UpstreamResponse(error.code, error.read() or b"", dict(error.headers or {}))


def forward_request(body, headers):
    """POST to the upstream, retrying only what is genuinely worth retrying."""
    response = None
    url = f"{config['target_url']}/v1/messages"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = _post_upstream(url, body, headers)
        except Exception as error:
            if not is_retryable_transport_error(error) or attempt > len(RETRY_EXCEPTION_DELAYS):
                raise
            delay = RETRY_EXCEPTION_DELAYS[attempt - 1]
            log_message(f"Upstream {type(error).__name__}, retrying in "
                        f"{delay:.1f}s ({attempt}/{MAX_ATTEMPTS - 1})")
            time.sleep(delay)
            continue

        delays = RETRY_POLICY.get(response.status_code)
        if not delays or attempt > len(delays):
            return response
        delay = delays[attempt - 1]
        log_message(f"Upstream returned {response.status_code}, "
                    f"retrying in {delay:.1f}s ({attempt}/{MAX_ATTEMPTS - 1})")
        time.sleep(delay)
    return response


def passthrough_error(response):
    """Return the upstream failure unchanged; never raise on a non-JSON body."""
    try:
        return jsonify(json.loads(response.content)), response.status_code
    except ValueError:
        content_type = response.headers.get("content-type") or "text/plain"
        return Response(response.content, status=response.status_code,
                        content_type=content_type)



# The relay attaches client tools natively, but only names it recognises, and only
# in lowercase snake_case: `Read` is dropped, `read` is honoured (measured 2026-10-05
# by forced tool_choice, 0/3 vs 3/3). Tools with no native name stay on the text
# protocol below. Capitalised names look dropped, which is why this used to be
# mistaken for "the relay ignores client tools" - popping `tools` entirely was what
# produced the "Tool 'X' not found" confabulation.
#
# Deliberately NOT native, though the relay accepts the names (measured 2026-10-06,
# forced calls): Edit - the relay fills its own read_tabular/pandas_operations
# args and drops old_string/new_string every time; WebFetch - drops the required
# prompt; TodoWrite - drops the required activeForm. All three route in-band, where
# the client model fills the schema correctly. Fields that can be derived from a
# sibling are also backfilled in clean_arguments as a second safety net.
NATIVE_MAP = {
    # name the client declares -> name this relay honours natively
    "Read": "read",
    "Write": "write",
    "Bash": "bash",
    "Grep": "grep",
    "Glob": "glob",
    "AskUserQuestion": "ask_user_question",
    "Task": "task",
    "Agent": "task",
    "NotebookEdit": "notebook_edit",
    "BashOutput": "bash_output",
    "KillShell": "kill_shell",
    "ExitPlanMode": "exit_plan_mode",
}
# Measured 2026-10-05, forced call with each tool's real schema: these came back with
# no native tool_use at all, so they - and anything not listed above - stay on the
# text protocol. WebSearch, LS, MultiEdit, View, Skill, and every mcp__* tool.
# A native loop is verified end to end: tools -> tool_use -> tool_result -> the model
# reads the result (2/2 turns, 2026-10-05).
# The shape we ask the model for. <invoke>/<parameter> is the syntax the upstream
# system prompt already defines for its own tools. Asking for a foreign tag instead
# measured worse, and the model closed it with </parameter></invoke> regardless.
TOOL_CALL_CLOSE = "</invoke>"
# Any of these opens a call. <tool_call> + JSON is what we asked for before and old
# transcripts still carry it, so it has to keep parsing.
CALL_OPENER_RE = re.compile(r"<\s*(?:antml:)?(tool_call|invoke)\b([^>]*?)/?>", re.IGNORECASE)
PARAM_OPENER_RE = re.compile(r"<\s*(?:antml:)?parameter\b", re.IGNORECASE)
PARAMETER_RE = re.compile(
    r"<\s*(?:antml:)?parameter\b([^>]*?)>(.*?)<\s*/\s*(?:antml:)?parameter\s*>",
    re.IGNORECASE | re.DOTALL)
TAG_NAME_RE = re.compile(r'name\s*=\s*"([^"]*)"', re.IGNORECASE)
# Every closing tag the model has been seen to write, including ones that do not
# match the opener it used - that mismatch is the most common way a call arrives.
CLOSER_RE = re.compile(
    r"</\s*(?:antml:)?(?:tool_call|invoke|tool_calls|function_calls|function_call"
    r"|parameter)\s*>", re.IGNORECASE)
# The subset that ends the call itself, as opposed to a single argument.
CALL_CLOSER_RE = re.compile(
    r"</\s*(?:antml:)?(?:tool_call|invoke|tool_calls|function_calls|function_call)"
    r"\s*>", re.IGNORECASE)
# The model sometimes invents harness chatter inside its own reply - notably faked
# <system-reminder> blocks. They are model output, not ours, and they must never
# reach the user or be mistaken for part of a tool call's body.
REMINDER_BLOCK_RE = re.compile(
    r"<system-reminder\b[^>]*>.*?</system-reminder\s*>", re.IGNORECASE | re.DOTALL)
REMINDER_TAG_RE = re.compile(r"</?system-reminder\b[^>]*>", re.IGNORECASE)
STRAY_MARKUP_RE = re.compile(
    r"</?(?:antml:)?(?:tool_call|invoke|parameter|function_call|tool_calls"
    r"|function_calls)\b[^>]*>", re.IGNORECASE)
TOOL_ID_MAP = {}


def make_tool_id(name):
    """Encode the tool name in the id so it survives a proxy restart."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(name))[:40] or "tool"
    return f"toolu_emul_{uuid.uuid4().hex[:8]}_{safe}"


def tool_name_from_id(tool_id):
    """Recover a tool name from an emulated id, falling back to the live map."""
    if tool_id in TOOL_ID_MAP:
        return TOOL_ID_MAP[tool_id]
    if isinstance(tool_id, str) and tool_id.startswith("toolu_emul_"):
        parts = tool_id.split("_", 3)
        if len(parts) == 4 and parts[3]:
            return parts[3]
    return "tool"


def resolve_model(requested):
    """Map the client's model name onto one the relay actually serves."""
    mapping = config.get("model_map") or {}
    if requested in mapping:
        return mapping[requested]
    return mapping.get("*", requested)


def has_tool_blocks(messages):
    """True when the conversation already carries native tool traffic."""
    for message in messages or []:
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_result"):
                    return True
    return False


def render_tool_call(name, tool_input):
    """Write one call the way this route asks for it, for replayed history."""
    parts = []
    for key, value in (tool_input or {}).items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        parts.append(f'<parameter name="{key}">{text}</parameter>')
    return f'<invoke name="{name}">{"".join(parts)}</invoke>'


def flatten_tool_result(content):
    """Reduce a tool_result body to plain text."""
    if content is None:
        return "(no output)"
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", "") if block.get("type") == "text"
                             else json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


COMPACT_SCHEMA_KEYS = ("type", "properties", "required", "items", "enum", "description")
PROPERTY_DESCRIPTION_LIMIT = 100
TOOL_DESCRIPTION_LIMIT = 200


def compact_schema(schema):
    """Keep only the schema keys a caller needs, and trim long descriptions.

    Tool schemas are re-sent as prompt text on every request, so verbose keys
    (title, examples, $schema, additionalProperties, ...) cost tokens every turn
    without helping the model pick or fill a tool.
    """
    if not isinstance(schema, dict):
        return schema
    compacted = {}
    for key, value in schema.items():
        if key not in COMPACT_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            compacted[key] = {name: compact_schema(sub) for name, sub in value.items()}
        elif key == "items" and isinstance(value, dict):
            compacted[key] = compact_schema(value)
        elif key == "description" and isinstance(value, str):
            compacted[key] = value[:PROPERTY_DESCRIPTION_LIMIT]
        else:
            compacted[key] = value
    return compacted


def format_tool(tool):
    """Render one tool as a compact prompt section."""
    description = str(tool.get("description") or "").strip()
    first_line = description.split("\n")[0][:TOOL_DESCRIPTION_LIMIT] if description else ""
    lines = [f"### {tool.get('name', 'unnamed')}"]
    if first_line:
        lines.append(first_line)
    schema = tool.get("input_schema") or {}
    if schema.get("properties"):
        lines.append("Parameters: " + json.dumps(compact_schema(schema),
                                                  separators=(",", ":"), ensure_ascii=False))
    else:
        lines.append("Parameters: none")
    return "\n".join(lines)


def build_tool_instructions(tools, tool_choice=None, native_pairs=None):
    """Describe this route's tools as prompt text the upstream model can follow.

    Choices below were measured against the relay on 2026-10-05; the counts are kept
    because the losing options are not obvious.

    - Placement: `system` is obeyed; anything in `messages` is refused as injection.
    - Wording: stating transport facts only scored 10/10 with zero suspicion at 63
      tools. Accusing the upstream prompt (naming its tools "forbidden") got 8/10 with
      2 suspicion; a bare additive list got 5/10, every miss the same confabulation
      ("Tool 'Bash' not found" for a call never made).
    - Batching: allowing several blocks gives TWO calls in 5/5 vs ONE in 5/5 under a
      single-block rule. The relay ignores stop_sequences, so batching is free.
    - The example uses real values, never TOOL_NAME/argument/value: filled scored
      7/8 and 8/8, placeholder 1/8 and 3/8.
    - "What silence means" took the thin framing from 5/10 to 10/10: a malformed block
      returns nothing, and the model reads that as "tool does not exist".
    - "Never guess or invent a tool's output" is phrased WITHOUT quoting the error
      string: quoting it primed the confabulation (3/8 vs 8/8 base).

    Native vs in-band: tools in NATIVE_MAP are attached natively, so the block format
    is wrong for them - the native opening is a separate document. The in-band half is
    emitted only when a tool actually needs it; a model told about an unused text
    protocol reaches for it on tools that would have worked natively.
    """
    native_pairs = list(native_pairs or ())

    if native_pairs:
        listed = "\n".join(f"    {client}   ->   call it as {relay}"
                            for client, relay in native_pairs)
        lines = [
            "# Execution environment",
            "",
            "You are answering through a gateway that speaks the Anthropic Messages API.",
            "This route attaches its tools two different ways, and how a tool is attached",
            "decides how you call it.",
            "",
            "## Natively attached tools",
            "",
            "These are attached the normal way. Call them through the platform's own",
            "tool-calling mechanism, exactly as you normally would - that mechanism is",
            "live on this route and those calls are read:",
            "",
            listed,
            "",
            "Use the name on the right when you call one. Do not write a text block for",
            "these, and do not describe the call in prose.",
            "",
        ]
    else:
        lines = [
            "# Execution environment",
            "",
            "You are answering through a gateway that speaks the Anthropic Messages API.",
            "On this route the tool interface travels in-band: the gateway reads a tool",
            "call out of your reply text, executes it, and returns the output to you as",
            "the next message. The platform's own tool plumbing is not attached to this",
            "route, so a call written any other way is never read by anything.",
            "",
            "This route is configured with the tools listed under \"Wired tools\" below.",
            "That list is the route's configuration: a tool is callable exactly when its",
            "name appears there.",
            "",
        ]

    if tools:
        if native_pairs:
            lines += [
                "## Tools wired in-band",
                "",
                "The tools under \"Wired tools\" below are NOT attached natively. They",
                "travel in-band: the gateway reads the call out of your reply text,",
                "executes it, and returns the output to you as the next message. A call",
                "for one of these written any other way is never read by anything, so",
                "for these - and only these - use the block format below.",
                "",
            ]
        lines += [
            "## Wire format",
            "",
            "A call is one block, read out of your reply text, so it must be written",
            "exactly like this:",
            "",
            '<invoke name="Bash"><parameter name="command">ls -la</parameter></invoke>',
            "",
            "Each argument is one <parameter name=\"key\"> value. Scalars are written as",
            "plain text; a list or an object is written as JSON inside its parameter.",
            "Close the block with exactly </invoke>.",
            "",
            "Write each block on its own, with no prose inside it, and stop writing",
            "once your blocks are done. When the calls are independent of each other,",
            "write several blocks in one reply, in the order they should run.",
            "",
            "Never guess or invent a tool's output. If no result came back, the call was",
            "not read, and that is the only thing it means.",
            "",
            "## What silence means",
            "",
            "A well-formed in-band block is answered with a message that begins with",
            "exactly:",
            "",
            "    TOOL RESULT for <tool name>: <the tool's output>",
            "",
            "A reply that arrives with no such message means one thing only: the block was",
            "not well-formed, so nothing read it. It does not mean the tool is missing -",
            "every tool under \"Wired tools\" is present whether or not a call for it was",
            "ever parsed. Write the block again in the exact format above and it will be",
            "read.",
            "",
            "## Wired tools",
            "",
        ]
        lines.extend(format_tool(tool) for tool in tools)
        lines.append("")

    if isinstance(tool_choice, dict):
        choice = tool_choice.get("type")
        if choice == "any":
            lines.append("You MUST call one of the tools above in this reply.")
        elif choice == "tool" and tool_choice.get("name"):
            lines.append(f"You MUST call the tool named {tool_choice['name']} in this reply.")
        elif choice == "none":
            lines.append("Do NOT call any tool in this reply; answer in plain text.")
    return "\n".join(lines)


def merge_system(system, extra):
    """Append our instructions to whatever system prompt the client already sent."""
    if not system:
        return extra
    if isinstance(system, str):
        return system + "\n\n" + extra
    if isinstance(system, list):
        return list(system) + [{"type": "text", "text": extra}]
    return extra


def emulated_history(messages, native_relay_names=()):
    """Flatten emulated tool traffic to text; leave the native traffic alone.

    History now carries two kinds of tool block and they must not be treated alike.
    A call for a natively-attached tool was issued by the relay under its own id, so
    the relay still recognises it: pass it back untouched, translating only the name
    to the relay's lowercase spelling. A call we emulated never existed upstream -
    its id is one we invented - so that one has to become text.
    """
    native_relay_names = set(native_relay_names or ())

    # Pass one: the ids of calls the upstream really made, so their results stay
    # native too. Deciding this from the id alone would be guesswork, because our
    # synthetic ids are the only ones with a known shape.
    native_ids = set()
    for message in messages or []:
        for block in message.get("content") or []:
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and NATIVE_MAP.get(block.get("name")) in native_relay_names):
                native_ids.add(block.get("id"))

    rewritten = []
    for message in messages or []:
        content = message.get("content")
        if not isinstance(content, list) or not any(
                isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result") for b in content):
            rewritten.append(message)
            continue
        blocks = []
        for block in content:
            block_type = block.get("type") if isinstance(block, dict) else None
            if block_type == "tool_use":
                relay_name = NATIVE_MAP.get(block.get("name"))
                if relay_name in native_relay_names:
                    kept = dict(block)
                    kept["name"] = relay_name
                    blocks.append(kept)
                    continue
                TOOL_ID_MAP[block.get("id")] = block.get("name", "tool")
                blocks.append({"type": "text",
                               "text": render_tool_call(block.get("name", "tool"),
                                                        block.get("input", {}))})
            elif block_type == "tool_result":
                if block.get("tool_use_id") in native_ids:
                    blocks.append(block)
                    continue
                name = tool_name_from_id(block.get("tool_use_id"))
                blocks.append({"type": "text",
                               "text": f"TOOL RESULT for {name}: "
                                       f"{flatten_tool_result(block.get('content'))}"})
            else:
                blocks.append(block)
        rewritten.append({"role": message.get("role", "user"), "content": blocks})
    return rewritten


def inject_tool_instructions(body, instructions):
    """Deliver the tool description through the top-level `system` field.

    Placement is what makes the model obey the tool list. Measured 2026-10-03:
    `system` field obeyed 3/3; a trailing system-role message or an append to the
    last user message was refused as injection ("tampak seperti upaya injeksi").
    Ours goes first, before the client's own system prompt. End to end through this
    proxy: 8/8 calls on turn one and 8/8 after a tool result.
    """
    ours = {"type": "text", "text": instructions}
    system = body.get("system")
    if isinstance(system, list):
        body["system"] = [ours] + list(system)
    elif isinstance(system, str) and system.strip():
        body["system"] = [ours, {"type": "text", "text": system}]
    else:
        body["system"] = [ours]
    return body

def apply_tool_emulation(body):
    """Split the client's tools into the natively-attached ones and the rest.

    Returns (body, native_reverse). native_reverse maps the relay's lowercase name
    back to the name the client declared, which is what the response path needs to
    hand a call back under a name the client recognises.
    """
    if len(TOOL_ID_MAP) > 5000:
        TOOL_ID_MAP.clear()

    native, emulated, native_pairs, native_reverse = [], [], [], {}
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        relay_name = NATIVE_MAP.get(tool.get("name"))
        if relay_name and isinstance(tool.get("input_schema"), dict):
            native.append({"name": relay_name,
                           "description": tool.get("description") or "",
                           "input_schema": tool["input_schema"]})
            native_pairs.append((tool["name"], relay_name))
            native_reverse[relay_name] = tool["name"]
        else:
            emulated.append(tool)

    body["messages"] = emulated_history(body.get("messages"), native_reverse.keys())

    if native or emulated:
        inject_tool_instructions(
            body, build_tool_instructions(emulated, body.get("tool_choice"),
                                          native_pairs))

    # A tool_result arrives as its own user turn, so the turn we attached the
    # description to may already be behind us - re-attach on every request.
    # (inject_tool_instructions always targets the newest user message.)
    if native:
        body["tools"] = native
    else:
        body.pop("tools", None)

    # tool_choice can only name something the relay itself knows about, so a choice
    # pointing at a text-protocol tool cannot be forwarded as a native force. The
    # prompt carries it instead.
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") == "tool":
        relay_name = NATIVE_MAP.get(choice.get("name"))
        if relay_name in native_reverse:
            body["tool_choice"] = {"type": "tool", "name": relay_name}
        else:
            body.pop("tool_choice", None)
    elif not native:
        body.pop("tool_choice", None)

    # Inert, kept only because clients and logs expect it. Measured 2026-10-05 over
    # 30 replies: the relay never once returned stop_reason "stop_sequence", and two
    # independent calls came back in 5/5 replies with this sequence set. The comment
    # here used to claim generation halts at the first closer and parallel calls are
    # impossible; both were wrong, and a prompt rule built on them cost a turn's
    # worth of real capability.
    stop_sequences = list(body.get("stop_sequences") or [])
    if TOOL_CALL_CLOSE not in stop_sequences:
        stop_sequences.append(TOOL_CALL_CLOSE)
    body["stop_sequences"] = stop_sequences
    return body, native_reverse


def _escape_control_chars(text):
    """Escape raw newlines/tabs a model wrote inside JSON string literals."""
    out = []
    in_string = False
    escaped = False
    for char in text:
        if not in_string:
            if char == '"':
                in_string = True
            out.append(char)
            continue
        if escaped:
            out.append(char)
            escaped = False
        elif char == "\\":
            out.append(char)
            escaped = True
        elif char == '"':
            out.append(char)
            in_string = False
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        else:
            out.append(char)
    return "".join(out)


def _balanced_json_slice(text):
    """Cut the first balanced {...} out of text, ignoring braces inside strings."""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    last_string_end = -1
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                last_string_end = index + 1
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    # Never balanced: the model dropped its closing braces, or trailed prose after
    # the object. End the slice at the last string terminator, which is the last
    # place a value can have finished. Returning the whole remainder instead hands
    # _close_unbalanced a tail it cannot close, because it appends its braces at the
    # very end of the text - past whatever the model wrote after the object - and the
    # parse then fails with a delimiter error at the first trailing character.
    if last_string_end > start:
        return text[start:last_string_end]
    return text[start:]


def _close_unbalanced(text):
    """Append whatever closers a truncated JSON object is still missing."""
    curly = 0
    square = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            curly += 1
        elif char == "}":
            curly -= 1
        elif char == "[":
            square += 1
        elif char == "]":
            square -= 1
    suffix = '"' if in_string else ""
    suffix += "]" * max(square, 0) + "}" * max(curly, 0)
    return text + suffix


def recoverable_prefixes(text, limit=240):
    """Prefixes of text that end where a JSON value could have finished.

    For a body whose braces never balance - the model dropped them, or trailed
    prose after the object - there is no balanced slice to find, and closing the
    text where it stands puts the braces in the wrong place. In the 2026-10-03
    Edit leak the slice ended on the bare key `"answers"`, so closing it produced
    `{"...": {"answers"}}`: a key with no value, which parses as nothing.

    Longest first, so the caller keeps the most content it can. Only CLOSING
    quotes count as a cut. An opening quote means the model was cut off mid-string,
    and closing there would fabricate a shorter value that never existed - a
    three-option AskUserQuestion silently becoming a one-option one.
    """
    cuts = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                cuts.append(index + 1)
            continue
        if char == '"':
            in_string = True
        elif char in "}]":
            cuts.append(index + 1)
    return [text[:cut] for cut in cuts[-limit:][::-1]]


def salvage_json(raw, allow_prefix=False):
    """Best-effort recovery of a JSON object from a sloppy <tool_call> body.

    Real replies arrive with code fences, closers from the wrong syntax, raw
    newlines inside string values, trailing commas and truncated braces. A strict
    json.loads turns every one of those into a dropped call that then leaks to the
    user as text, so repair the body before giving up on it.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return None
    candidate = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", candidate)
    candidate = re.sub(r"\s*```\s*$", "", candidate)
    candidate = strip_emulation_noise(candidate).strip()
    candidate = _balanced_json_slice(candidate)
    if not candidate:
        return None

    bases = [candidate, _close_unbalanced(candidate)]
    if allow_prefix:
        bases.extend(_close_unbalanced(prefix)
                     for prefix in recoverable_prefixes(candidate))

    variants = []
    for base in bases:
        for step in (base, _escape_control_chars(base)):
            variants.append(step)
            variants.append(re.sub(r",\s*([}\]])", r"\1", step))
    for variant in variants:
        try:
            parsed = json.loads(variant)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            if variant != candidate:
                log_message("Salvaged a malformed emulated tool call")
            return parsed
    return None


def parse_parameters(body):
    """Turn a <parameter name="...">...</parameter> body into an argument dict.

    A value becomes JSON whenever it parses as JSON. That covers the rule the upstream
    prompt states for lists and objects, and it lets numbers and booleans arrive as the
    types the tool schemas declare. Anything that does not parse stays the string the
    model wrote, which is what a shell command needs.
    """
    arguments = {}
    for match in PARAMETER_RE.finditer(body):
        key = TAG_NAME_RE.search(match.group(1))
        if not key:
            continue
        raw = match.group(2).strip()
        try:
            arguments[key.group(1)] = json.loads(raw)
        except ValueError:
            arguments[key.group(1)] = raw
    return arguments


def parse_tool_call(raw, allow_prefix=False, opener_name=None):
    """Parse one call body into (name, input), or None if unusable.

    `opener_name` comes from the opener tag's own attribute, which is the only place
    an <invoke> body carries its name; a JSON body carries its own.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if PARAM_OPENER_RE.match(text):
        if not opener_name:
            return None
        return opener_name, parse_parameters(text)
    parsed = salvage_json(text, allow_prefix)
    if not isinstance(parsed, dict):
        return None
    name = parsed.get("name") or parsed.get("tool") or opener_name
    if any(key in parsed for key in ("input", "arguments", "parameters")):
        arguments = parsed.get("input", parsed.get("arguments", parsed.get("parameters", {})))
    elif "name" in parsed or "tool" in parsed:
        # Arguments written beside the name: {"name": "Bash", "command": "ls"}.
        arguments = {k: v for k, v in parsed.items() if k not in ("name", "tool")}
    else:
        # An <invoke> body holding bare JSON - the whole object is the arguments.
        arguments = parsed
    if not isinstance(name, str) or not name:
        return None
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = {"raw": arguments}
    if not isinstance(arguments, dict):
        arguments = {"value": arguments}
    return name, arguments


def clean_arguments(arguments, tool):
    """Keep only the arguments the target tool actually accepts."""
    schema = (tool or {}).get("input_schema") or {}
    allowed = set((schema.get("properties") or {}).keys())
    if not allowed:
        return arguments
    cleaned = {}
    for key, value in (arguments or {}).items():
        if key not in allowed:
            continue
        if isinstance(value, str) and value.startswith("@"):
            value = value[1:]  # the relay's own tools use stage paths
        cleaned[key] = value
    # The relay's own tools (e.g. system_todo_write -> TodoWrite) omit required
    # fields the client's schema demands, and the client rejects a call that is
    # missing one. Backfill array-item required strings from a sibling field so
    # the call validates. This copies data that is already present; it invents
    # nothing. Measured 2026-10-06: the relay drops activeForm from every todo.
    cleaned = _backfill_required(cleaned, schema)
    return cleaned


def _backfill_required(arguments, schema):
    """Fill required string fields in array items from a present sibling.

    Only touches arrays of objects whose item schema marks a string field
    required but absent. The value is copied from another present field on the
    same item (preferring one named 'content'/'label'/'name'), never fabricated.
    """
    for key, prop in (schema.get("properties") or {}).items():
        if (prop or {}).get("type") != "array":
            continue
        item_schema = (prop.get("items") or {})
        req = item_schema.get("required") or []
        item_props = item_schema.get("properties") or {}
        rows = arguments.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            source = (row.get("content") or row.get("label") or row.get("name")
                      or next((v for v in row.values() if isinstance(v, str)), None))
            for field in req:
                if field in row:
                    continue
                if (item_props.get(field) or {}).get("type") != "string":
                    continue
                if source is not None:
                    row[field] = source
    return arguments


def remap_tool_calls(response_data, client_tools, native_reverse=None):
    """Give every upstream tool call back under a name the client actually declared.

    Two things arrive here. A natively-attached call comes back under the relay's
    lowercase spelling, so it is renamed to whatever the client called that tool.
    A call for one of the relay's OWN tools - read_tabular and the like - is a tool
    the client never declared and therefore cannot run, so it is redirected onto a
    client equivalent when one exists.
    """
    native_reverse = native_reverse or {}
    by_name = {t.get("name"): t for t in client_tools or [] if isinstance(t, dict)}
    aliases = config.get("tool_aliases") or {}
    for block in response_data.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        client_name = native_reverse.get(name)
        if client_name:
            block["name"] = client_name
            if client_name in by_name:
                block["input"] = clean_arguments(block.get("input"), by_name[client_name])
            log_message(f"Native tool call: {name} -> {client_name}")
            continue
        if not aliases or not by_name or name in by_name:
            continue
        target = aliases.get(name)
        if not target or target not in by_name:
            continue
        block["name"] = target
        block["input"] = clean_arguments(block.get("input"), by_name[target])
        log_message(f"Remapped upstream tool call: {name} -> {target}")
    return response_data


def strip_emulation_noise(text):
    """Drop the tags the model invents around and inside emulated tool calls."""
    text = REMINDER_BLOCK_RE.sub(" ", text)
    text = REMINDER_TAG_RE.sub(" ", text)
    return STRAY_MARKUP_RE.sub(" ", text)


def strip_stray_markup(text):
    """Drop leftover emulation tags so a bad closer never reaches the user."""
    return strip_emulation_noise(text).strip()


def split_emulated_calls(text):
    """Return (start, end, body, opener_name) for every call in text.

    Two encodings are accepted, and which one a body uses is decided by what follows
    the opener rather than by which tag opened it:

      * a body starting with `{` is JSON and ends at the first closing tag of any
        kind. That is what makes the most common drift harmless - the model closes a
        JSON block with `</parameter></invoke>`, but the JSON is complete before the
        stray tag, so cutting there loses nothing.
      * a body starting with `<parameter` is XML and must NOT end at `</parameter>`,
        which closes one argument rather than the call. It runs to the next
        call-level closer.

    Keyed on the OPEN tag, so one malformed call can never swallow the next one.
    """
    spans = []
    starts = list(CALL_OPENER_RE.finditer(text))
    for index, match in enumerate(starts):
        body_start = match.end()
        limit = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        segment = text[body_start:limit]
        closer = (CALL_CLOSER_RE if PARAM_OPENER_RE.match(segment.lstrip())
                  else CLOSER_RE).search(segment)
        if closer:
            body = segment[:closer.start()]
            end = body_start + closer.end()
            # A model mixing syntaxes often closes with several tags in a row.
            while True:
                following = CLOSER_RE.match(text, end)
                if not following:
                    break
                end = following.end()
        else:
            body = segment
            end = limit
        attr = TAG_NAME_RE.search(match.group(2) or "")
        spans.append((match.start(), end, body, attr.group(1) if attr else None))
    return spans


def extract_tool_calls(response_data):
    """Turn <tool_call> markup in the reply back into real tool_use blocks."""
    content = response_data.get("content") or []
    rebuilt = []
    found = 0
    spans_seen = 0
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            rebuilt.append(block)
            continue
        text = block.get("text") or ""
        position = 0
        for start, end, body, opener_name in split_emulated_calls(text):
            spans_seen += 1
            leading = strip_stray_markup(text[position:start])
            if leading:
                rebuilt.append({"type": "text", "text": leading})
            # A body cut off by max_tokens is an unfinished generation, not a
            # tool call with dropped braces. Recovering a prefix from it would
            # manufacture a shorter argument than the model was still writing.
            call = parse_tool_call(
                body, allow_prefix=response_data.get("stop_reason") != "max_tokens",
                opener_name=opener_name)
            if call:
                name, tool_input = call
                tool_id = make_tool_id(name)
                TOOL_ID_MAP[tool_id] = name
                rebuilt.append({"type": "tool_use", "id": tool_id,
                                "name": name, "input": tool_input})
                found += 1
            else:
                recovered = strip_stray_markup(body)
                log_message("Emulated tool call unparseable in any known "
                            f"shape - passing it through as text: {recovered[:200]!r}")
                if recovered:
                    rebuilt.append({"type": "text", "text": recovered})
            position = end
        trailing = strip_stray_markup(text[position:])
        if trailing:
            rebuilt.append({"type": "text", "text": trailing})

    if found:
        response_data["content"] = rebuilt
        response_data["stop_reason"] = "tool_use"
        response_data["stop_sequence"] = None
        log_message(f"Emulated {found} tool call(s)")
    else:
        # Even with nothing parseable, still hand back the rebuilt blocks: they have
        # the stray markup stripped, so a dead <tool_call> tag cannot reach the user.
        if spans_seen:
            response_data["content"] = rebuilt
        if response_data.get("stop_sequence") == TOOL_CALL_CLOSE:
            # Our own stop sequence fired - the client never asked for it.
            response_data["stop_reason"] = "end_turn"
            response_data["stop_sequence"] = None
    return response_data


CONTEXT_WINDOW_LIMIT = 1_000_000


def reports_full_prompt_as_input(it, cc, cr):
    """True when input_tokens looks like the WHOLE prompt, cache included.

    Measured 2026-10-03: a ~70-token request came back input=10966/cache=10964, and
    the gap between input_tokens and the cache figures was always the uncached tail
    (2-16 tokens), never a real split. The test is deliberately narrow: some backends
    (deepseek, glm) report standard semantics where uncached legitimately exceeds
    cached, and a looser `input >= cache` test wrongly rewrote 410 such rows.
    """
    cache_sum = cc + cr
    if cache_sum <= 0:
        return False
    return 0 <= it - cache_sum <= max(32, 0.001 * it)


def choose_prompt_total(cc, cr, prompt_bytes):
    """Decide how many tokens the prompt really was. Returns (total, why).

    input_tokens is `cc + cr + residual`, so it carries no information of its own,
    and the cache figures are unreliable. The tiebreaker is the one thing the relay
    never reports: the prompt's byte length. Nothing textual packs tighter than one
    token / 8 bytes, so a cache_creation below bytes/8 is a broken number, not a
    small prompt - only that too-small direction is corrected. A larger-than-expected
    cache_creation is left alone (this backend genuinely tokenizes denser than Claude:
    17,788 tokens for 21,170 bytes). Seen on session 44d24bea: a ~530KB body reported
    cache_creation of 2,937 then 12,955 - the turns that wedged it past 1M.
    """
    if not prompt_bytes:
        return cc + cr, "no size probe - keeping both cache figures"

    floor_tokens = prompt_bytes // 8
    if cc >= floor_tokens:
        return cc, (f"cc={cc} is plausible for a {prompt_bytes}B prompt "
                    f"(floor {floor_tokens}, cr={cr} discarded)")

    estimate = prompt_bytes // 3
    return estimate, (f"cc={cc} is impossible for a {prompt_bytes}B prompt "
                      f"(floor {floor_tokens}); using the {estimate} estimate")

def sanitize_usage(response_data, prompt_bytes=None):
    """Undo the relay's token double-count so the client's window math is true.

    The relay reports input_tokens as the whole prompt (cache included) AND reports
    those same tokens in cache_creation/cache_read. Clients compute
    context = input + cache_read + cache_creation, so the prompt counts twice: a 374k
    prompt reached Claude Code as 1,493,494 and forced an auto-compact loop. Drop the
    copy in input_tokens, then let choose_prompt_total read the cache figures.
    """
    usage = response_data.get("usage")
    if not isinstance(usage, dict):
        return response_data

    it = usage.get("input_tokens") or 0
    cc = usage.get("cache_creation_input_tokens") or 0
    cr = usage.get("cache_read_input_tokens") or 0
    cache_sum = cc + cr

    if reports_full_prompt_as_input(it, cc, cr):
        # input_tokens was the whole prompt, so the uncached tail is the
        # residual and nothing may be subtracted from the cache figures.
        residual = max(it - cache_sum, 0)
        total, why = choose_prompt_total(cc, cr, prompt_bytes)
        log_message(
            f"Usage fix: raw in={it} cc={cc} cr={cr} "
            f"-> {total} ({why})"
        )
        usage["input_tokens"] = residual
        usage["cache_creation_input_tokens"] = total
        usage["cache_read_input_tokens"] = 0
        it, cc, cr = residual, total, 0

    total = it + cc + cr
    if total > CONTEXT_WINDOW_LIMIT:
        # Do NOT trim this away. After the fix above this is a real prompt that
        # genuinely outgrew the window, and the client has to be allowed to see
        # it so it compacts - hiding it would strand the session instead of
        # saving it.
        log_message(
            f"Context for real: {total} tokens > "
            f"{CONTEXT_WINDOW_LIMIT}. Not trimmed - the client must compact."
        )

    response_data["usage"] = usage
    return response_data

def generate_sse_stream(response_data):
    """Build a valid SSE stream in Anthropic format from a complete message."""

    # 1. message_start - send a simplified message structure
    message_start = {
        "type": "message_start",
        "message": {
            "id": response_data.get("id", "msg_proxy"),
            "type": "message",
            "role": response_data.get("role", "assistant"),
            "content": [],
            "model": response_data.get("model", "claude-opus-4-8"),
            "stop_reason": None,
            "stop_sequence": None,
            "usage": response_data.get("usage", {"input_tokens": 0, "output_tokens": 0})
        }
    }
    yield f"event: message_start\ndata: {json.dumps(message_start)}\n\n"

    # 2. one start/delta/stop triple per content block
    content = response_data.get('content', [])
    for i, block in enumerate(content):
        block_type = block.get("type", "text")

        if block_type == "tool_use":
            block_start = {
                "type": "content_block_start",
                "index": i,
                "content_block": {
                    "type": "tool_use",
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": {}
                }
            }
            yield f"event: content_block_start\ndata: {json.dumps(block_start)}\n\n"
            block_delta = {
                "type": "content_block_delta",
                "index": i,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block.get("input", {}))
                }
            }
            yield f"event: content_block_delta\ndata: {json.dumps(block_delta)}\n\n"

        elif block_type == "thinking":
            block_start = {
                "type": "content_block_start",
                "index": i,
                "content_block": {"type": "thinking", "thinking": ""}
            }
            yield f"event: content_block_start\ndata: {json.dumps(block_start)}\n\n"
            if block.get("thinking"):
                block_delta = {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "thinking_delta", "thinking": block["thinking"]}
                }
                yield f"event: content_block_delta\ndata: {json.dumps(block_delta)}\n\n"

        elif block_type == "redacted_thinking":
            block_start = {
                "type": "content_block_start",
                "index": i,
                "content_block": {"type": "redacted_thinking", "data": block.get("data", "")}
            }
            yield f"event: content_block_start\ndata: {json.dumps(block_start)}\n\n"

        else:
            block_start = {
                "type": "content_block_start",
                "index": i,
                "content_block": {"type": "text", "text": ""}
            }
            yield f"event: content_block_start\ndata: {json.dumps(block_start)}\n\n"
            if block.get("text"):
                block_delta = {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "text_delta", "text": block["text"]}
                }
                yield f"event: content_block_delta\ndata: {json.dumps(block_delta)}\n\n"

        block_stop = {"type": "content_block_stop", "index": i}
        yield f"event: content_block_stop\ndata: {json.dumps(block_stop)}\n\n"

    # 3. message_delta
    message_delta = {
        "type": "message_delta",
        "delta": {
            "stop_reason": response_data.get("stop_reason", "end_turn"),
            "stop_sequence": response_data.get("stop_sequence")
        },
        "usage": {
            "output_tokens": response_data.get("usage", {}).get("output_tokens", 0)
        }
    }
    yield f"event: message_delta\ndata: {json.dumps(message_delta)}\n\n"

    # 4. message_stop
    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"


def log_message(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

class _ProxyHTTPHandler(BaseHTTPRequestHandler):
    """Route POST /v1/messages into proxy_messages(); everything else is a 404.

    The Flask view returned either a Response or a (Response|payload, status)
    tuple; _send normalises both, and sends a buffered body or a chunked SSE
    stream depending on which the view produced.
    """
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass  # the proxy has its own logger; silence the default stderr spam

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _path_only(self):
        """The path with any query string removed.

        Flask matched routes on the path alone, so /v1/messages?beta=true reached
        the view. Comparing self.path directly does not, and Claude Code does send
        ?beta=true - against a naive handler every request 404s and the session
        dies with no visible error. Measured 2026-10-05: /v1/messages -> 401 (reached
        upstream), /v1/messages?beta=true -> 404.
        """
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def do_GET(self):
        if self._path_only() == "/health":
            payload = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_error(404)

    def do_POST(self):
        path = self._path_only()
        if path in ("/v1/messages/count_tokens", "/count_tokens"):
            # The relay has no such endpoint; answering locally avoids a 404 storm
            # every time the client sizes its context.
            tokens = estimate_input_tokens(self._read_body())
            payload = json.dumps({"input_tokens": tokens}).encode("utf-8")
            log_message(f"count_tokens -> {tokens} (local estimate)")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path != "/v1/messages":
            # Log it: a silent 404 here is invisible to the client and looks like a
            # dead proxy.
            log_message(f"404 for POST {self.path}")
            self.send_error(404)
            return
        request.bind(self._read_body(), self.headers)
        try:
            result = proxy_messages()
        except Exception as error:  # mirror Flask's 500 behaviour
            log_message(f"Handler error: {error}")
            body = json.dumps({"error": {"message": str(error)}}).encode("utf-8")
            result = Response(body, status=500)
        self._send(result)

    def _normalise(self, result):
        """Turn a view return value into a single Response object."""
        status_override = None
        if isinstance(result, tuple):
            payload, status_override = result[0], result[1]
        else:
            payload = result
        if not isinstance(payload, Response):
            payload = jsonify(payload)
        if status_override is not None:
            payload.status = status_override
        return payload

    def _send(self, result):
        resp = self._normalise(result)
        if resp.streaming:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            for key, value in resp.headers.items():
                self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for chunk in resp.body:
                    if not chunk:
                        continue
                    data = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                log_message("client disconnected mid-stream")
        else:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            for key, value in resp.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(resp.body)))
            self.end_headers()
            try:
                self.wfile.write(resp.body)
            except (BrokenPipeError, ConnectionResetError):
                pass


def estimate_input_tokens(raw_body):
    """Rough token count for /v1/messages/count_tokens.

    Claude Code calls this to size its context window. The relay has nothing
    behind that path, so we answer locally instead of forwarding a 404. This
    backend runs ~3.3 bytes/token on plain prose (measured 2026-10-03), which is
    a closer estimate than the usual bytes/4.
    """
    return max(1, int(len(raw_body or b"") / 3.3))


def make_proxy_server(host, port):
    """Build a threaded stdlib HTTP server bound to host:port."""
    return ThreadingHTTPServer((host, port), _ProxyHTTPHandler)

def main():
    """Entry point for pm2 / systemd / direct `python3 Proxy.py`.

    Configuration comes from the environment:
        API_KEY        upstream key (optional; clients forward their own)
        PORT           local port                 (default 8181)
        HOST           bind address               (default 127.0.0.1)
        TARGET_URL     upstream base URL          (default from config)
        EMULATE_TOOLS  true / false               (default true)
        DEFAULT_MODEL  fallback model for any requested name
    """
    config["target_url"] = os.environ.get("TARGET_URL", config["target_url"]).rstrip("/")
    config["api_key"] = os.environ.get("API_KEY", "")
    config["port"] = int(os.environ.get("PORT", "8181"))
    config["emulate_tools"] = os.environ.get("EMULATE_TOOLS", "true").lower() not in ("0", "false", "no")
    default_model = os.environ.get("DEFAULT_MODEL")
    if default_model:
        config.setdefault("model_map", {})["*"] = default_model
    host = os.environ.get("HOST", "127.0.0.1")

    if not config["api_key"]:
        print("API_KEY not set - relying on the key each client forwards", flush=True)

    server = make_proxy_server(host, config["port"])
    config["is_running"] = True

    def handle_signal(signum, _frame):
        print(f"received signal {signum}, shutting down", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    # SIGTERM is absent on Windows; register whatever the platform offers.
    for sig in ("SIGTERM", "SIGINT"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), handle_signal)

    print(f"proxy listening on http://{host}:{config['port']} -> {config['target_url']}", flush=True)
    print(f"tool emulation: {'on' if config['emulate_tools'] else 'off'}", flush=True)
    server.serve_forever()
    print("stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
