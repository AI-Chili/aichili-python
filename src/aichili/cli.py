"""Zero-dependency CLI (`aichili`) for AI Chili Studio & API and OnlyChili Cinema."""

from __future__ import annotations

import json
import math
import sys
from typing import Any, Callable

from .client import (
    VERSION,
    AiChiliClient,
    AiChiliError,
    TransportCallable,
    estimate_cost,
)
from .mcp import start_mcp_stdio_server

HELP_TEXT = f"""AI Chili Official Python CLI (aichili v{VERSION})
https://aichili.ai | https://aichili.ai/developers

Usage:
  aichili <command> [options]

Commands:
  models [--scope available|console] [--json]
  estimate [--seconds 90] [--resolution 768p] [--model h3-vdn] [--workflow talk|photo] [--script-units N]
           [--free-used] [--subscribers N] [--sub-price 9.99] [--ppv-count N] [--ppv-price 4.99] [--json]
  credits [--api-key chili_sk_...] [--json]
  plan --model <id> --mode <t2v|i2v|t2i|i2i> --prompt "..." [--duration 15] [--resolution 768p]
       [--ratio 16:9] [--workflow general] [--seed N] [--ref-asset <uuid>]... [--ref-role <role>]...
       [--upscale-4k] [--no-optimize-prompt] [--json]
  generate --model <id> --mode <t2v|i2v|t2i|i2i> --prompt "..." [--duration 15] [--resolution 768p]
           [--ratio 16:9] [--workflow general] [--seed N] [--ref-asset <uuid>]... [--ref-role <role>]...
           [--upscale-4k] [--no-optimize-prompt] [--idempotency-key <key>] [--wait]
           [--poll-interval 5] [--timeout 600] [--json]
  status <job_id> [--api-key chili_sk_...] [--json]
  mcp [--base-url <url>]

Global Flags:
  --api-key <key>     Override AICHILI_API_KEY (must start with chili_sk_)
  --base-url <url>    Override AICHILI_API_BASE (default: https://videoapi.aichili.ai)
  --json              Emit machine-readable JSON on stdout/stderr
  -h, --help          Show help
  -v, --version       Show CLI version
"""

VALUE_FLAGS = frozenset(
    {
        "--scope",
        "--seconds",
        "--resolution",
        "--model",
        "--mode",
        "--prompt",
        "--duration",
        "--ratio",
        "--workflow",
        "--seed",
        "--script-units",
        "--subscribers",
        "--sub-price",
        "--ppv-count",
        "--ppv-price",
        "--idempotency-key",
        "--ref-asset",
        "--ref-role",
        "--poll-interval",
        "--poll-interval-s",
        "--poll-interval-ms",
        "--timeout",
        "--timeout-s",
        "--timeout-ms",
        "--api-key",
        "--base-url",
    }
)

BOOLEAN_FLAGS = frozenset(
    {
        "--json",
        "--help",
        "-h",
        "--version",
        "-v",
        "--wait",
        "--free-used",
        "--upscale-4k",
        "--no-optimize-prompt",
    }
)


def _looks_like_flag_token(token: str) -> bool:
    if token.startswith("--"):
        return True
    return token in BOOLEAN_FLAGS or token in VALUE_FLAGS


def parse_cli_args(
    argv: list[str],
) -> tuple[dict[str, Any], dict[str, list[str]], list[str]]:
    flags: dict[str, Any] = {}
    multi_flags: dict[str, list[str]] = {"--ref-asset": [], "--ref-role": []}
    positionals: list[str] = []

    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            positionals.extend(argv[i + 1 :])
            break

        if token.startswith("-") and token != "-":
            eq_index = token.find("=")
            key = token if eq_index == -1 else token[:eq_index]
            inline_value = None if eq_index == -1 else token[eq_index + 1 :]

            if key in BOOLEAN_FLAGS:
                if inline_value is not None:
                    raise AiChiliError(
                        "INVALID_ARGS",
                        f"Boolean flag '{key}' does not accept a value.",
                    )
                flags[key] = True
                i += 1
                continue

            if key in VALUE_FLAGS:
                if inline_value is not None:
                    next_val = inline_value
                else:
                    if i + 1 >= len(argv) or _looks_like_flag_token(argv[i + 1]):
                        raise AiChiliError(
                            "INVALID_ARGS",
                            f"Flag '{key}' requires a value.",
                        )
                    i += 1
                    next_val = argv[i]

                if key in multi_flags:
                    multi_flags[key].append(next_val)
                else:
                    flags[key] = next_val
                i += 1
                continue

            raise AiChiliError(
                "INVALID_ARGS",
                f"Unknown flag '{key}'. Run 'aichili --help' for usage.",
            )

        positionals.append(token)
        i += 1

    return flags, multi_flags, positionals


def _parse_optional_int(raw: str | None, flag_name: str) -> int | None:
    if raw is None:
        return None
    try:
        # Reject floats like "15.5" cleanly.
        if "." in raw or "e" in raw.lower():
            raise ValueError(raw)
        return int(raw, 10)
    except ValueError as exc:
        raise AiChiliError(
            "INVALID_ARGS",
            f"Flag '{flag_name}' must be an integer (received '{raw}').",
        ) from exc


def _parse_optional_number(raw: str | None, flag_name: str) -> float | None:
    if raw is None:
        return None
    try:
        num = float(raw)
    except ValueError as exc:
        raise AiChiliError(
            "INVALID_ARGS",
            f"Flag '{flag_name}' must be a valid number (received '{raw}').",
        ) from exc
    if not math.isfinite(num):
        raise AiChiliError(
            "INVALID_ARGS",
            f"Flag '{flag_name}' must be a valid number (received '{raw}').",
        )
    return num


def _build_generation_kwargs(
    flags: dict[str, Any],
    multi_flags: dict[str, list[str]],
) -> dict[str, Any]:
    model_id = flags.get("--model")
    mode = flags.get("--mode")
    prompt = flags.get("--prompt")
    if not model_id or not mode or prompt is None:
        raise AiChiliError(
            "INVALID_ARGS",
            'Missing required flags: --model <id>, --mode <t2v|i2v|t2i|i2i>, and --prompt "..." are required.',
        )

    params: dict[str, Any] = {"prompt": prompt}
    duration = _parse_optional_int(flags.get("--duration"), "--duration")
    if duration is not None:
        params["duration_s"] = duration
    if flags.get("--resolution") is not None:
        params["resolution"] = flags["--resolution"]
    if flags.get("--ratio") is not None:
        params["ratio"] = flags["--ratio"]
    seed = _parse_optional_int(flags.get("--seed"), "--seed")
    if seed is not None:
        params["seed"] = seed
    if multi_flags["--ref-asset"]:
        params["ref_asset_ids"] = list(multi_flags["--ref-asset"])
    if multi_flags["--ref-role"]:
        params["ref_roles"] = list(multi_flags["--ref-role"])

    return {
        "mode": mode,
        "model_id": model_id,
        "params": params,
        "workflow": flags.get("--workflow"),
        "upscale_4k": True if flags.get("--upscale-4k") else None,
        "optimize_prompt": False if flags.get("--no-optimize-prompt") else None,
    }


def _print_output(
    data: Any,
    json_mode: bool,
    human_formatter: Callable[[Any], str],
    stdout: Any,
) -> None:
    if json_mode:
        stdout.write(f"{json.dumps(data, indent=2, allow_nan=False)}\n")
    else:
        stdout.write(f"{human_formatter(data)}\n")


def _print_error(err: Exception, json_mode: bool, stderr: Any) -> None:
    if isinstance(err, AiChiliError):
        payload: dict[str, Any] = {
            "code": err.code,
            "message": err.message,
            "status": err.status,
        }
    else:
        payload = {
            "code": "CLI_ERROR",
            "message": str(err),
            "status": None,
        }

    if json_mode:
        stderr.write(f"{json.dumps({'error': payload}, allow_nan=False)}\n")
    else:
        status_suffix = f" (HTTP {payload['status']})" if payload.get("status") else ""
        stderr.write(f"[ERROR] {payload['code']}{status_suffix}: {payload['message']}\n")


def _resolve_poll_timing(flags: dict[str, Any]) -> tuple[float, float]:
    raw_poll_s = (
        flags["--poll-interval"]
        if "--poll-interval" in flags
        else flags.get("--poll-interval-s")
    )
    poll_s = _parse_optional_number(raw_poll_s, "--poll-interval")
    poll_ms = _parse_optional_number(flags.get("--poll-interval-ms"), "--poll-interval-ms")
    interval_s = poll_s if poll_s is not None else (poll_ms / 1000.0 if poll_ms is not None else 5.0)

    raw_timeout_s = (
        flags["--timeout"]
        if "--timeout" in flags
        else flags.get("--timeout-s")
    )
    timeout_s = _parse_optional_number(raw_timeout_s, "--timeout")
    timeout_ms = _parse_optional_number(flags.get("--timeout-ms"), "--timeout-ms")
    effective_timeout_s = (
        timeout_s if timeout_s is not None else (timeout_ms / 1000.0 if timeout_ms is not None else 600.0)
    )
    return interval_s, effective_timeout_s


def main(
    argv: list[str] | None = None,
    *,
    stdout: Any = None,
    stderr: Any = None,
    transport: TransportCallable | None = None,
) -> int:
    """Entry point for the `aichili` CLI."""
    args = list(sys.argv[1:] if argv is None else argv)
    out = sys.stdout if stdout is None else stdout
    err_out = sys.stderr if stderr is None else stderr
    wants_json_early = "--json" in args

    try:
        flags, multi_flags, positionals = parse_cli_args(args)
        json_mode = bool(flags.get("--json"))

        if flags.get("--version") or flags.get("-v"):
            _print_output({"name": "aichili", "version": VERSION}, json_mode, lambda _: VERSION, out)
            return 0

        if flags.get("--help") or flags.get("-h") or len(positionals) == 0:
            if len(positionals) == 0 and not flags.get("--help") and not flags.get("-h"):
                raise AiChiliError(
                    "INVALID_ARGS",
                    "No command specified. Run 'aichili --help' for usage.",
                )
            out.write(HELP_TEXT)
            return 0

        command, *rest = positionals

        if command in ("mcp", "aichili-mcp"):
            if rest:
                raise AiChiliError("INVALID_ARGS", "Usage: aichili mcp [--base-url <url>]")
            start_mcp_stdio_server(base_url=flags.get("--base-url"))
            return 0

        client = AiChiliClient(
            api_key=flags.get("--api-key"),
            base_url=flags.get("--base-url"),
            transport=transport,
        )

        if command == "models":
            if rest:
                raise AiChiliError("INVALID_ARGS", "Usage: aichili models [--scope available|console] [--json]")
            result = client.list_models(scope=flags.get("--scope"))

            def _fmt_models(res: dict[str, Any]) -> str:
                models = res.get("models") if isinstance(res, dict) and isinstance(res.get("models"), list) else []
                lines = [f"Live AI Chili Models ({len(models)}):"]
                for m in models:
                    dur_range = f"{m.get('single_call_durations_min', '?')}-{m.get('single_call_durations_max', '?')}s"
                    seen_modes: list[str] = []
                    for wf in m.get("workflows") or []:
                        if not wf.get("hidden"):
                            for md in wf.get("modes") or []:
                                if md not in seen_modes:
                                    seen_modes.append(md)
                    lines.append(
                        f"  - {m.get('model_id')} ({m.get('display_name')}) | provider={m.get('provider')} | {dur_range} | modes={','.join(seen_modes)}"
                    )
                return "\n".join(lines)

            _print_output(result, json_mode, _fmt_models, out)
            return 0

        if command == "estimate":
            if rest:
                raise AiChiliError("INVALID_ARGS", "Unexpected positional arguments for 'estimate'.")
            parsed_seconds = _parse_optional_number(flags.get("--seconds"), "--seconds")
            parsed_subs = _parse_optional_number(flags.get("--subscribers"), "--subscribers")
            parsed_sub_price = _parse_optional_number(flags.get("--sub-price"), "--sub-price")
            parsed_ppv_count = _parse_optional_number(flags.get("--ppv-count"), "--ppv-count")
            parsed_ppv_price = _parse_optional_number(flags.get("--ppv-price"), "--ppv-price")
            est = estimate_cost(
                seconds=parsed_seconds if parsed_seconds is not None else 15,
                resolution=flags.get("--resolution") if flags.get("--resolution") is not None else "768p",
                model_id=flags.get("--model") if flags.get("--model") is not None else "h3-vdn",
                workflow=flags.get("--workflow") if flags.get("--workflow") is not None else "talk",
                script_units=_parse_optional_number(flags.get("--script-units"), "--script-units"),
                free_signup_credits_used=bool(flags.get("--free-used")),
                subscribers=parsed_subs if parsed_subs is not None else 0,
                sub_price=parsed_sub_price if parsed_sub_price is not None else 9.99,
                ppv_count=parsed_ppv_count if parsed_ppv_count is not None else 0,
                ppv_price=parsed_ppv_price if parsed_ppv_price is not None else 4.99,
            )

            def _fmt_est(e: dict[str, Any]) -> str:
                lines = [
                    f"AI Chili Cost Estimate ({e['rateBasis']['modelId']}):",
                    f"  Total Credits:        {e['totalCredits']}",
                    f"  Free Sign-Up Applied: -{e['freeCreditsApplied']} (25 free sign-up credits)",
                    f"  Billable Credits:     {e['billableCredits']}",
                    f"  Studio Effective USD: ${float(e['effectiveUsdAtStudioRate']):.2f} ($49.90 / 780 cr)",
                    f"  Recommended Pack:     {e['recommendedPack']['name']} (${float(e['recommendedPack']['checkoutSpendUsd']):.2f})",
                ]
                proj = e["onlyChiliProjection"]
                if proj["grossRevenueUsd"] > 0:
                    lines.append(
                        f"  OnlyChili 80% Net:    ${float(proj['creatorNetRevenueUsd']):.2f} (across {proj['languagesSupported']} languages)"
                    )
                return "\n".join(lines)

            _print_output(est, json_mode, _fmt_est, out)
            return 0

        if command == "credits":
            if rest:
                raise AiChiliError("INVALID_ARGS", "Usage: aichili credits [--api-key chili_sk_...] [--json]")
            credits_res = client.get_credits()
            _print_output(
                credits_res,
                json_mode,
                lambda cr: f"Balance: {cr.get('balance')} credits | Plan: {cr.get('plan') or 'free'} | PAYG Unlocked: {bool(cr.get('payg_unlocked'))}",
                out,
            )
            return 0

        if command == "plan":
            if rest:
                raise AiChiliError("INVALID_ARGS", "Unexpected positional arguments for 'plan'.")
            req_kwargs = _build_generation_kwargs(flags, multi_flags)
            plan_res = client.plan_generation(**req_kwargs)
            _print_output(
                plan_res,
                json_mode,
                lambda plan: (
                    f"Plan Quote: {plan.get('total_credits')} credits | "
                    f"{plan.get('total_duration_s')}s across {plan.get('segments')} segment(s)"
                    f"{' (estimated)' if plan.get('estimated') else ''}"
                ),
                out,
            )
            return 0

        if command == "generate":
            if rest:
                raise AiChiliError("INVALID_ARGS", "Unexpected positional arguments for 'generate'.")
            req_kwargs = _build_generation_kwargs(flags, multi_flags)
            job = client.create_generation(
                **req_kwargs,
                idempotency_key=flags.get("--idempotency-key"),
            )
            if flags.get("--wait"):
                interval_s, timeout_s = _resolve_poll_timing(flags)
                job = client.wait_for_generation(
                    job["id"],
                    poll_interval_s=interval_s,
                    timeout_s=timeout_s,
                )
            _print_output(
                job,
                json_mode,
                lambda j: f"Generation {j.get('id')}: status={j.get('status')} | model={j.get('model_id')} | credits={j.get('credits_cost')}",
                out,
            )
            if flags.get("--wait") and str(job.get("status", "")).lower() in (
                "failed",
                "cancelled",
                "canceled",
            ):
                return 1
            return 0

        if command == "status":
            if len(rest) != 1:
                raise AiChiliError("INVALID_ARGS", "Usage: aichili status <job_id> [--json]")
            job = client.get_generation(rest[0])
            _print_output(
                job,
                json_mode,
                lambda j: f"Generation {j.get('id')}: status={j.get('status')} | model={j.get('model_id')} | credits={j.get('credits_cost')}",
                out,
            )
            return 0

        raise AiChiliError(
            "UNKNOWN_COMMAND",
            f"Unknown command '{command}'. Run 'aichili --help' for usage.",
        )
    except Exception as exc:
        _print_error(exc, wants_json_early, err_out)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
