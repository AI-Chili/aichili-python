"""Zero-dependency newline-delimited JSON-RPC 2.0 stdio MCP server for AI Chili."""

from __future__ import annotations

import copy
import io
import json
import sys
from typing import Any, BinaryIO, TextIO

from .client import (
    VERSION,
    AiChiliError,
    TransportCallable,
    create_generation,
    estimate_cost,
    get_credits,
    get_generation,
    get_platform_overview,
    list_models,
    plan_generation,
)

SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {
        "2025-11-25",
        "2025-06-18",
        "2025-03-26",
        "2024-11-05",
    }
)
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
MAX_STDIO_LINE_BYTES = 1_048_576  # 1 MiB
_DRAIN_CHUNK_SIZE = 65_536

MCP_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "platform_overview",
        "title": "AI Chili & OnlyChili Platform Overview",
        "description": "Return canonical metadata for AI Chili Studio & API, the 5 public video/avatar models, PAYG credit packs, API docs URLs, and OnlyChili Cinema creator monetization terms.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "list_video_models",
        "title": "List Live AI Chili Video & Avatar Models",
        "description": "Fetch live capabilities, durations, resolutions, aspect ratios, and credit pricing rules from GET /v1/registry on videoapi.aichili.ai.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["available", "console"],
                    "description": "Optional registry scope ('available' default or 'console').",
                }
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "estimate_video_cost",
        "title": "Estimate AI Chili PAYG Credits & OnlyChili Creator Earnings",
        "description": "Deterministically calculate PAYG credit usage, recommended credit packs ($0 Free 25 cr, $9.90 Starter 150 cr, $29.90 Creator 460 cr, $49.90 Studio & API 780 cr), and optional OnlyChili Cinema 80% net creator revenue share.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "Target total video duration in seconds (default 15).",
                },
                "resolution": {
                    "type": "string",
                    "enum": ["480p", "768p", "1080p", "2k"],
                    "description": "Output resolution for h3-vdn rate estimation (default '768p').",
                },
                "model_id": {
                    "type": "string",
                    "enum": ["h3-vdn", "h3-sol", "avatar-talk"],
                    "description": "Model ID ('h3-vdn' / 'h3-sol' default, or 'avatar-talk').",
                },
                "workflow": {
                    "type": "string",
                    "enum": ["talk", "photo"],
                    "description": "For avatar-talk: 'photo' adds +10 base-clip credits; 'talk' adds 0.",
                },
                "script_units": {
                    "type": "number",
                    "description": "Script units for avatar-talk (billed at 0.2 credits per 5 script units, plus +10 on workflow 'photo').",
                },
                "free_signup_credits_used": {
                    "type": "boolean",
                    "description": "Set true if the 25 free sign-up credits have already been spent.",
                },
                "subscribers": {
                    "type": "number",
                    "description": "Projected OnlyChili Cinema monthly subscribers.",
                },
                "sub_price": {
                    "type": "number",
                    "description": "OnlyChili monthly membership price in USD ($2.99-$99.99).",
                },
                "sub_price_usd": {
                    "type": "number",
                    "description": "Alias for sub_price (monthly membership price in USD).",
                },
                "ppv_count": {
                    "type": "number",
                    "description": "Projected OnlyChili pay-per-view unlocks.",
                },
                "ppv_unlocks": {
                    "type": "number",
                    "description": "Alias for ppv_count (monthly PPV episode unlocks).",
                },
                "ppv_price": {
                    "type": "number",
                    "description": "OnlyChili PPV unlock price in USD ($0.99-$500.00).",
                },
                "ppv_price_usd": {
                    "type": "number",
                    "description": "Alias for ppv_price (PPV unlock price in USD).",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "get_credit_balance",
        "title": "Get AI Chili Credit Wallet Balance",
        "description": "Fetch live credit wallet balance, daily grant status, and PAYG tier state via GET /v1/credits. Requires chili_sk_... API key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "api_key": {
                    "type": "string",
                    "description": "Optional AI Chili API key (chili_sk_...). Defaults to AICHILI_API_KEY env var.",
                }
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "plan_video_generation",
        "title": "Dry-Run & Price a Video Generation Plan",
        "description": "Validate parameters and quote exact segment count, duration, and total credits via POST /v1/generations/plan without charging credits or enqueueing a job.",
        "inputSchema": {
            "type": "object",
            "required": ["mode", "model_id"],
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["t2v", "i2v", "t2i", "i2i"],
                    "description": "Generation mode (e.g. 't2v' or 'i2v').",
                },
                "model_id": {
                    "type": "string",
                    "description": "Model ID (e.g. 'h3-vdn', 'wan3.0-video', 'seedance-2.5', 'minimax-h3-gmi', 'avatar-talk').",
                },
                "prompt": {
                    "type": "string",
                    "description": "Scene, motion, camera, and audio prompt.",
                },
                "duration_s": {
                    "type": "integer",
                    "description": "Requested duration in seconds.",
                },
                "resolution": {
                    "type": "string",
                    "description": "Output resolution matching the selected model's registry entry.",
                },
                "ratio": {
                    "type": "string",
                    "description": "Aspect ratio (e.g. '16:9', '9:16', '1:1', '3:4', '4:3', '21:9').",
                },
                "workflow": {
                    "type": "string",
                    "description": "Optional model workflow ID (e.g. 'general', 'reference', 'photo', 'talk').",
                },
                "ref_asset_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional uploaded reference image/video asset UUIDs for i2v.",
                },
                "ref_roles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional roles matching ref_asset_ids (reference_image, reference_video, first_frame, last_frame).",
                },
                "params": {
                    "type": "object",
                    "description": "Optional raw GenerationParamsIn object (merged with top-level prompt/duration_s/resolution/ratio).",
                },
                "upscale_4k": {
                    "type": "boolean",
                    "description": "Include 4K upscale in the dry-run quote.",
                },
                "optimize_prompt": {
                    "type": "boolean",
                    "description": "Enable prompt optimization (default true on server).",
                },
                "api_key": {
                    "type": "string",
                    "description": "Optional AI Chili API key (chili_sk_...). Defaults to AICHILI_API_KEY env var.",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "create_video_generation",
        "title": "Submit an AI Chili Video or Avatar Generation",
        "description": "Validate, charge credits, and enqueue a video or talking-avatar generation job via POST /v1/generations. Supports idempotency_key for safe retries.",
        "inputSchema": {
            "type": "object",
            "required": ["mode", "model_id"],
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["t2v", "i2v", "t2i", "i2i"],
                    "description": "Generation mode (e.g. 't2v' or 'i2v').",
                },
                "model_id": {
                    "type": "string",
                    "description": "Model ID (e.g. 'h3-vdn', 'wan3.0-video', 'seedance-2.5', 'minimax-h3-gmi', 'avatar-talk').",
                },
                "prompt": {
                    "type": "string",
                    "description": "Scene, motion, camera, and audio prompt.",
                },
                "duration_s": {
                    "type": "integer",
                    "description": "Requested duration in seconds.",
                },
                "resolution": {
                    "type": "string",
                    "description": "Output resolution matching the selected model's registry entry.",
                },
                "ratio": {
                    "type": "string",
                    "description": "Aspect ratio (e.g. '16:9', '9:16', '1:1', '3:4', '4:3', '21:9').",
                },
                "workflow": {
                    "type": "string",
                    "description": "Optional model workflow ID (e.g. 'general', 'reference', 'photo', 'talk').",
                },
                "ref_asset_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional uploaded reference image/video asset UUIDs for i2v.",
                },
                "ref_roles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional roles matching ref_asset_ids (reference_image, reference_video, first_frame, last_frame).",
                },
                "params": {
                    "type": "object",
                    "description": "Optional raw GenerationParamsIn object (merged with top-level prompt/duration_s/resolution/ratio).",
                },
                "upscale_4k": {
                    "type": "boolean",
                    "description": "Request 4K upscale on output.",
                },
                "optimize_prompt": {
                    "type": "boolean",
                    "description": "Enable prompt optimization.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional idempotency key so retries return the original job without double charging.",
                },
                "api_key": {
                    "type": "string",
                    "description": "Optional AI Chili API key (chili_sk_...). Defaults to AICHILI_API_KEY env var.",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "get_video_generation",
        "title": "Get AI Chili Generation Job Status & Output",
        "description": "Poll job status, segment progress, credit cost, and output asset metadata via GET /v1/generations/{job_id}.",
        "inputSchema": {
            "type": "object",
            "required": ["job_id"],
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Generation job UUID returned by create_video_generation.",
                },
                "api_key": {
                    "type": "string",
                    "description": "Optional AI Chili API key (chili_sk_...). Defaults to AICHILI_API_KEY env var.",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
)


def _first_not_none(*values: Any, default: Any = None) -> Any:
    for val in values:
        if val is not None:
            return val
    return default


def _merge_mcp_generation_params(args: dict[str, Any]) -> dict[str, Any]:
    raw_params = args.get("params")
    base = dict(raw_params) if isinstance(raw_params, dict) else {}
    if args.get("prompt") is not None:
        base["prompt"] = args["prompt"]
    if args.get("duration_s") is not None:
        base["duration_s"] = args["duration_s"]
    if args.get("resolution") is not None:
        base["resolution"] = args["resolution"]
    if args.get("ratio") is not None:
        base["ratio"] = args["ratio"]
    ref_assets = args.get("ref_asset_ids")
    if isinstance(ref_assets, list) and len(ref_assets) > 0:
        base["ref_asset_ids"] = list(ref_assets)
    ref_roles = args.get("ref_roles")
    if isinstance(ref_roles, list) and len(ref_roles) > 0:
        base["ref_roles"] = list(ref_roles)
    return base


def _format_tool_success(payload: Any) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, allow_nan=False)}],
        "structuredContent": payload,
        "isError": False,
    }


def _format_tool_error(err: Exception) -> dict[str, Any]:
    if isinstance(err, AiChiliError):
        error_payload: dict[str, Any] = {
            "code": err.code,
            "message": err.message,
            "status": err.status,
            "details": err.details,
        }
    else:
        error_payload = {
            "code": "TOOL_EXECUTION_ERROR",
            "message": str(err),
        }
    return {
        "content": [
            {"type": "text", "text": json.dumps({"error": error_payload}, indent=2, allow_nan=False)}
        ],
        "isError": True,
    }


def dispatch_tool_call(
    params: Any,
    *,
    base_url: str | None = None,
    transport: TransportCallable | None = None,
) -> dict[str, Any]:
    """Dispatch a single MCP `tools/call` invocation."""
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _format_tool_error(
            AiChiliError("INVALID_TOOL_CALL", "Tool call requires a string 'name'.")
        )

    raw_args = params.get("arguments")
    args = raw_args if isinstance(raw_args, dict) else {}
    tool_name = params["name"]

    try:
        if tool_name == "platform_overview":
            return _format_tool_success(get_platform_overview())

        if tool_name == "list_video_models":
            return _format_tool_success(
                list_models(scope=args.get("scope"), base_url=base_url, transport=transport)
            )

        if tool_name == "estimate_video_cost":
            return _format_tool_success(
                estimate_cost(
                    seconds=_first_not_none(args.get("seconds"), default=15),
                    resolution=_first_not_none(args.get("resolution"), default="768p"),
                    model_id=_first_not_none(args.get("model_id"), args.get("modelId"), default="h3-vdn"),
                    workflow=_first_not_none(args.get("workflow"), default="talk"),
                    script_units=_first_not_none(args.get("script_units"), args.get("scriptUnits")),
                    free_signup_credits_used=bool(
                        _first_not_none(
                            args.get("free_signup_credits_used"),
                            args.get("freeSignupCreditsUsed"),
                            default=False,
                        )
                    ),
                    subscribers=_first_not_none(args.get("subscribers"), default=0),
                    sub_price=_first_not_none(
                        args.get("sub_price"),
                        args.get("sub_price_usd"),
                        args.get("subPrice"),
                        default=9.99,
                    ),
                    ppv_count=_first_not_none(
                        args.get("ppv_count"),
                        args.get("ppv_unlocks"),
                        args.get("ppvCount"),
                        default=0,
                    ),
                    ppv_price=_first_not_none(
                        args.get("ppv_price"),
                        args.get("ppv_price_usd"),
                        args.get("ppvPrice"),
                        default=4.99,
                    ),
                )
            )

        if tool_name == "get_credit_balance":
            return _format_tool_success(
                get_credits(
                    api_key=_first_not_none(args.get("api_key"), args.get("apiKey")),
                    base_url=base_url,
                    transport=transport,
                )
            )

        if tool_name == "plan_video_generation":
            return _format_tool_success(
                plan_generation(
                    api_key=_first_not_none(args.get("api_key"), args.get("apiKey")),
                    mode=args.get("mode", ""),
                    model_id=_first_not_none(args.get("model_id"), args.get("modelId"), default=""),
                    params=_merge_mcp_generation_params(args),
                    workflow=args.get("workflow"),
                    upscale_4k=args.get("upscale_4k"),
                    optimize_prompt=args.get("optimize_prompt"),
                    base_url=base_url,
                    transport=transport,
                )
            )

        if tool_name == "create_video_generation":
            return _format_tool_success(
                create_generation(
                    api_key=_first_not_none(args.get("api_key"), args.get("apiKey")),
                    mode=args.get("mode", ""),
                    model_id=_first_not_none(args.get("model_id"), args.get("modelId"), default=""),
                    params=_merge_mcp_generation_params(args),
                    workflow=args.get("workflow"),
                    upscale_4k=args.get("upscale_4k"),
                    optimize_prompt=args.get("optimize_prompt"),
                    idempotency_key=_first_not_none(
                        args.get("idempotency_key"), args.get("idempotencyKey")
                    ),
                    base_url=base_url,
                    transport=transport,
                )
            )

        if tool_name == "get_video_generation":
            return _format_tool_success(
                get_generation(
                    _first_not_none(args.get("job_id"), args.get("jobId"), default=""),
                    api_key=_first_not_none(args.get("api_key"), args.get("apiKey")),
                    base_url=base_url,
                    transport=transport,
                )
            )

        return _format_tool_error(
            AiChiliError("UNKNOWN_TOOL", f"Unknown MCP tool '{tool_name}'.")
        )
    except Exception as exc:
        return _format_tool_error(exc)


def handle_jsonrpc_message(
    message: Any,
    *,
    base_url: str | None = None,
    transport: TransportCallable | None = None,
) -> dict[str, Any] | None:
    """Process a single parsed JSON-RPC 2.0 request/notification object."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        msg_id = message.get("id") if isinstance(message, dict) and "id" in message else None
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32600, "message": "Invalid JSON-RPC 2.0 request."},
        }

    if "id" not in message:
        # JSON-RPC 2.0 notifications must never emit a response frame.
        return None

    msg_id = message["id"]
    method = message.get("method")
    params = message.get("params")

    if not isinstance(method, str):
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32600, "message": "Invalid JSON-RPC 2.0 request."},
        }

    if method == "initialize":
        requested = (
            params.get("protocolVersion")
            if isinstance(params, dict) and isinstance(params.get("protocolVersion"), str)
            else DEFAULT_PROTOCOL_VERSION
        )
        protocol_version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        )
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "io.github.AI-Chili/aichili",
                    "title": "AI Chili Video Generation Studio & API",
                    "version": VERSION,
                },
                "instructions": "Inspect live AI Chili video/avatar models, estimate PAYG credits & OnlyChili Cinema creator earnings, dry-run generation quotes, submit jobs, and poll status. Set AICHILI_API_KEY (chili_sk_...) for authenticated endpoints.",
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": copy.deepcopy(list(MCP_TOOLS))},
        }

    if method == "tools/call":
        result = dispatch_tool_call(params, base_url=base_url, transport=transport)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _reject_nonstandard_json_constant(token: str) -> Any:
    raise ValueError(f"Non-standard JSON constant not allowed: {token}")


def _write_frame(output: TextIO | BinaryIO, payload: Any) -> None:
    serialized = f"{json.dumps(payload, allow_nan=False)}\n"
    if isinstance(output, io.TextIOBase) or hasattr(output, "encoding"):
        output.write(serialized)  # type: ignore[arg-type]
    else:
        output.write(serialized.encode("utf-8"))  # type: ignore[arg-type]
    output.flush()


def _iter_bounded_lines(
    stream: TextIO | BinaryIO,
    max_line_bytes: int = MAX_STDIO_LINE_BYTES,
):
    """Yield `(line_str, error_code_message)` with O(1) memory cap on arbitrarily long lines."""
    is_text = isinstance(stream, io.TextIOBase) or hasattr(stream, "encoding")
    read_limit = max_line_bytes + 2

    while True:
        chunk = stream.readline(read_limit)
        if not chunk:
            break

        if is_text:
            text_chunk = chunk  # type: ignore[assignment]
            has_newline = text_chunk.endswith("\n")
            content = text_chunk.rstrip("\r\n")
            if len(content.encode("utf-8")) > max_line_bytes or (
                not has_newline and len(text_chunk) >= read_limit
            ):
                while not has_newline:
                    tail = stream.readline(_DRAIN_CHUNK_SIZE)
                    if not tail:
                        break
                    has_newline = tail.endswith("\n")  # type: ignore[union-attr]
                yield None, "Parse error: JSON-RPC line exceeds 1 MiB limit"
                continue
            yield content, None
        else:
            byte_chunk: bytes = chunk  # type: ignore[assignment]
            has_newline = byte_chunk.endswith(b"\n")
            content_bytes = byte_chunk.rstrip(b"\r\n")
            if len(content_bytes) > max_line_bytes or (
                not has_newline and len(byte_chunk) >= read_limit
            ):
                while not has_newline:
                    tail_b = stream.readline(_DRAIN_CHUNK_SIZE)
                    if not tail_b:
                        break
                    has_newline = tail_b.endswith(b"\n")  # type: ignore[union-attr]
                yield None, "Parse error: JSON-RPC line exceeds 1 MiB limit"
                continue
            try:
                decoded = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                yield None, "Parse error: invalid UTF-8"
                continue
            yield decoded, None


def start_mcp_stdio_server(
    *,
    input_stream: TextIO | BinaryIO | None = None,
    output_stream: TextIO | BinaryIO | None = None,
    base_url: str | None = None,
    transport: TransportCallable | None = None,
) -> None:
    """Run the synchronous newline-delimited JSON-RPC 2.0 MCP server until EOF."""
    in_stream: TextIO | BinaryIO
    if input_stream is not None:
        in_stream = input_stream
    elif hasattr(sys.stdin, "buffer"):
        in_stream = sys.stdin.buffer
    else:
        in_stream = sys.stdin

    out_stream: TextIO | BinaryIO = output_stream if output_stream is not None else sys.stdout

    for line, line_err in _iter_bounded_lines(in_stream, MAX_STDIO_LINE_BYTES):
        if line_err is not None:
            _write_frame(
                out_stream,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": line_err},
                },
            )
            continue

        assert line is not None
        trimmed = line.strip()
        if not trimmed:
            continue

        try:
            parsed = json.loads(trimmed, parse_constant=_reject_nonstandard_json_constant)
        except ValueError:
            _write_frame(
                out_stream,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error: invalid JSON"},
                },
            )
            continue

        if isinstance(parsed, list):
            if len(parsed) == 0:
                _write_frame(
                    out_stream,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Invalid JSON-RPC 2.0 request."},
                    },
                )
                continue
            responses = []
            for item in parsed:
                res = handle_jsonrpc_message(item, base_url=base_url, transport=transport)
                if res is not None:
                    responses.append(res)
            if responses:
                _write_frame(out_stream, responses)
            continue

        response = handle_jsonrpc_message(parsed, base_url=base_url, transport=transport)
        if response is not None:
            _write_frame(out_stream, response)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `aichili-mcp` console script."""
    args = list(sys.argv[1:] if argv is None else argv)
    base_url: str | None = None
    i = 0
    while i < len(args):
        token = args[i]
        if token in ("-v", "--version"):
            sys.stdout.write(f"{VERSION}\n")
            return 0
        if token in ("-h", "--help"):
            sys.stdout.write(
                f"AI Chili Stdio MCP Server (aichili-mcp v{VERSION})\n"
                "Usage: aichili-mcp [--base-url <url>] [--version] [--help]\n"
            )
            return 0
        if token == "--base-url":
            if i + 1 >= len(args):
                sys.stderr.write("[ERROR] INVALID_ARGS: Flag '--base-url' requires a value.\n")
                return 1
            base_url = args[i + 1]
            i += 2
            continue
        if token.startswith("--base-url="):
            base_url = token.split("=", 1)[1]
            i += 1
            continue
        sys.stderr.write(f"[ERROR] INVALID_ARGS: Unknown flag '{token}'.\n")
        return 1

    start_mcp_stdio_server(base_url=base_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
