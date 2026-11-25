"""Core zero-dependency AI Chili HTTP client, pricing calculator, and SSOT metadata."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable

VERSION = "1.0.1"
DEFAULT_API_BASE = "https://videoapi.aichili.ai"
USER_AGENT = f"aichili-python/{VERSION} (+https://aichili.ai/developers)"

FREE_SIGNUP_CREDITS = 25

CREDIT_PACKS: tuple[dict[str, Any], ...] = (
    {"id": "free-signup", "name": "Free Sign-Up", "priceUsd": 0, "credits": 25},
    {"id": "starter", "name": "Starter", "priceUsd": 9.9, "credits": 150},
    {"id": "creator", "name": "Creator", "priceUsd": 29.9, "credits": 460},
    {"id": "studio-api", "name": "Studio & API", "priceUsd": 49.9, "credits": 780},
)

H3_VDN_RATES_PER_SECOND: dict[str, float] = {
    "480p": 0.6,
    "768p": 1.0,
    "1080p": 1.8,
    "2k": 2.5,
}

AVATAR_TALK_PRICING: dict[str, Any] = {
    "creditsPerBlock": 0.2,
    "blockUnits": 5,
    "photoBaseClipCredits": 10,
    "minDurationSeconds": 3,
    "maxDurationSeconds": 300,
    "resolutions": ["768p", "source"],
}

PUBLIC_MODELS: tuple[dict[str, Any], ...] = (
    {
        "modelId": "h3-vdn",
        "aliases": ["h3-sol"],
        "displayName": "MiniMax H3 \u00b7 SOL NSFW",
        "provider": "comfyui",
        "modes": ["t2v", "i2v"],
        "minDurationSeconds": 5,
        "maxDurationSeconds": 15,
        "resolutions": ["480p", "768p", "1080p", "2k"],
        "audio": True,
        "pricingSummary": "0.6 cr/s (480p), 1.0 cr/s (768p), 1.8 cr/s (1080p), 2.5 cr/s (2k), rounded up to whole credits",
    },
    {
        "modelId": "minimax-h3-gmi",
        "displayName": "MiniMax H3",
        "provider": "gmi",
        "modes": ["t2v", "i2v"],
        "minDurationSeconds": 4,
        "maxDurationSeconds": 15,
        "resolutions": ["768P", "2K"],
        "audio": True,
        "pricingSummary": "Live quote via POST /v1/generations/plan",
    },
    {
        "modelId": "wan3.0-video",
        "displayName": "Wan 3.0",
        "provider": "gmi",
        "modes": ["t2v", "i2v"],
        "minDurationSeconds": 2,
        "maxDurationSeconds": 30,
        "resolutions": ["480P", "720P", "1080P"],
        "audio": True,
        "pricingSummary": "Live quote via POST /v1/generations/plan",
    },
    {
        "modelId": "seedance-2.5",
        "displayName": "Seedance 2.5",
        "provider": "gmi",
        "modes": ["t2v", "i2v"],
        "minDurationSeconds": 4,
        "maxDurationSeconds": 15,
        "resolutions": ["480p", "720p", "1080p"],
        "audio": True,
        "pricingSummary": "Live quote via POST /v1/generations/plan",
    },
    {
        "modelId": "avatar-talk",
        "displayName": "Avatar Talk",
        "provider": "comfyui",
        "modes": ["i2v"],
        "minDurationSeconds": 3,
        "maxDurationSeconds": 300,
        "resolutions": ["768p", "source"],
        "audio": True,
        "pricingSummary": "0.2 credits per 5 script units (+10 base credits on workflow 'photo')",
    },
)

ONLYCHILI_CINEMA: dict[str, Any] = {
    "name": "OnlyChili Cinema",
    "url": "https://onlychili.aichili.ai",
    "mcpUrl": "https://onlychili.aichili.ai/mcp",
    "creatorNetRevenueSharePct": 80,
    "platformFeePct": 20,
    "languagesSupported": 47,
    "membershipPriceRangeUsd": {"min": 2.99, "max": 99.99},
    "ppvPriceRangeUsd": {"min": 0.99, "max": 500.0},
}

VALID_MODES = frozenset({"t2v", "i2v", "t2i", "i2i"})
VALID_SCOPES = frozenset({"available", "console"})
TERMINAL_STATUSES = frozenset({"succeeded", "completed", "failed", "cancelled", "canceled"})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
VENDOR_QUOTED_MODELS = frozenset(
    {
        "wan3.0-video",
        "wan-3",
        "seedance-2.5",
        "seedance-2-5",
        "minimax-h3-gmi",
        "minimax-h3",
    }
)

_API_KEY_PATTERN = re.compile(r"chili_sk_[A-Za-z0-9_-]{8,}\Z")
_JOB_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_CONTROL_OR_SPACE_RE = re.compile(r"[\s\x00-\x1f\x7f]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

MAX_HTTP_RESPONSE_BYTES = 16 * 1024 * 1024

TransportCallable = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


class AiChiliError(Exception):
    """Structured SDK error preserving error code, HTTP status, and parsed response details."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        details: Any = None,
        resolution_hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details
        self.resolution_hint = resolution_hint


def _round_js(value: float, decimals: int = 4) -> float | int:
    """Match Node.js `Math.round((value + Number.EPSILON) * 10**d) / 10**d` half-up behavior."""
    factor = 10**decimals
    rounded = math.floor((value + sys.float_info.epsilon) * factor + 0.5) / factor
    if math.isfinite(rounded) and rounded.is_integer():
        return int(rounded)
    return rounded


def _clean_num(value: float | int) -> float | int:
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return value


def _to_finite_number(raw: Any, error_code: str, error_message: str) -> float:
    if isinstance(raw, bool) or raw is None:
        raise AiChiliError(error_code, error_message)
    try:
        num = float(raw)
    except (TypeError, ValueError) as exc:
        raise AiChiliError(error_code, error_message) from exc
    if not math.isfinite(num):
        raise AiChiliError(error_code, error_message)
    return num


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse cross-origin or HTTPS->HTTP downgrade redirects so Bearer tokens never leak."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        orig = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        if (
            target.scheme.lower() != orig.scheme.lower()
            or target.netloc.lower() != orig.netloc.lower()
        ):
            raise AiChiliError(
                "INSECURE_REDIRECT",
                f"Refusing cross-origin or protocol-changing redirect from {orig.scheme}://{orig.netloc} to {target.scheme}://{target.netloc}.",
                status=code,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body_bytes: bytes | None,
    timeout_s: float,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url=url, data=body_bytes, headers=headers, method=method)
    ssl_ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl_ctx),
        _SafeRedirectHandler(),
    )
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            status = int(getattr(resp, "status", None) or resp.getcode())
            data = resp.read(MAX_HTTP_RESPONSE_BYTES + 1)
            if len(data) > MAX_HTTP_RESPONSE_BYTES:
                raise AiChiliError(
                    "RESPONSE_TOO_LARGE",
                    f"Response from {url} exceeded {MAX_HTTP_RESPONSE_BYTES} bytes.",
                )
            return status, data
    except AiChiliError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            err_bytes = exc.read(MAX_HTTP_RESPONSE_BYTES)
        finally:
            exc.close()
        return int(exc.code), err_bytes
    except Exception as exc:
        raise AiChiliError(
            "NETWORK_ERROR",
            f"Request to {url} failed: {exc}",
        ) from exc


def resolve_base_url(base_url: str | None = None) -> str:
    """Validate and normalize the AI Chili API base URL."""
    source = base_url if base_url is not None else (os.environ.get("AICHILI_API_BASE") or DEFAULT_API_BASE)
    if not isinstance(source, str):
        raise AiChiliError("INVALID_BASE_URL", "Invalid API base URL.")
    raw = source.strip()
    if not raw or _CONTROL_OR_SPACE_RE.search(raw):
        raise AiChiliError("INVALID_BASE_URL", f"Invalid API base URL: {raw!r}")

    parsed = urllib.parse.urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in ("https", "http"):
        raise AiChiliError("INVALID_BASE_URL", f"Unsupported API protocol: {parsed.scheme or 'missing'}")
    if not parsed.hostname:
        raise AiChiliError("INVALID_BASE_URL", f"Invalid API base URL: {raw}")
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or "?" in raw
        or "#" in raw
        or parsed.query
        or parsed.fragment
    ):
        raise AiChiliError(
            "INVALID_BASE_URL",
            "API base URL must not include embedded credentials, query parameters, or hash fragments.",
        )

    try:
        _ = parsed.port
    except ValueError as exc:
        raise AiChiliError("INVALID_BASE_URL", f"Invalid port in API base URL: {raw}") from exc

    host_lower = parsed.hostname.lower()
    origin = f"{scheme}://{parsed.netloc}"
    if scheme == "http" and host_lower not in LOOPBACK_HOSTS:
        raise AiChiliError(
            "INSECURE_BASE_URL",
            f"Refusing cleartext HTTP API base URL '{origin}'. Use https:// (http:// is permitted only on localhost/loopback).",
        )

    clean_path = "" if parsed.path in ("", "/") else parsed.path.rstrip("/")
    return f"{origin}{clean_path}"


def validate_api_key(api_key: str | None = None) -> str:
    """Validate an AI Chili API key (`chili_sk_[A-Za-z0-9_-]{8,}`) without echoing raw key values."""
    if api_key is not None and not isinstance(api_key, str):
        raise AiChiliError(
            "INVALID_API_KEY",
            "Malformed AI Chili API key: expected 'chili_sk_' prefix followed by at least 8 alphanumeric characters.",
        )
    resolved = api_key if isinstance(api_key, str) else os.environ.get("AICHILI_API_KEY", "")
    trimmed = resolved.strip()
    if not trimmed:
        raise AiChiliError(
            "INVALID_API_KEY",
            "Missing AI Chili API key. Pass api_key / --api-key or set AICHILI_API_KEY (must start with chili_sk_).",
        )
    if _API_KEY_PATTERN.fullmatch(trimmed) is None:
        raise AiChiliError(
            "INVALID_API_KEY",
            "Malformed AI Chili API key: expected 'chili_sk_' prefix followed by at least 8 alphanumeric characters.",
        )
    return trimmed


def validate_job_id(job_id: str) -> str:
    """Enforce strict single-segment `^[A-Za-z0-9_-]{1,128}$` job_id before network serialization."""
    if not isinstance(job_id, str) or not job_id:
        raise AiChiliError("INVALID_JOB_ID", "Job ID must be a non-empty UUID or slug.")
    if ".." in job_id or _JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise AiChiliError(
            "INVALID_JOB_ID",
            "Invalid job_id: expected UUID or safe alphanumeric slug without path separators, percent-encoding, or whitespace.",
        )
    return job_id


def _validate_generation_request(
    mode: str,
    model_id: str,
    params: dict[str, Any] | None,
) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(mode, str) or mode not in VALID_MODES:
        expected = ", ".join(["t2v", "i2v", "t2i", "i2i"])
        raise AiChiliError(
            "INVALID_MODE",
            f"Invalid generation mode '{mode}'. Expected one of: {expected}.",
        )
    if (
        not isinstance(model_id, str)
        or ".." in model_id
        or _MODEL_ID_PATTERN.fullmatch(model_id.strip()) is None
    ):
        raise AiChiliError(
            "INVALID_MODEL_ID",
            "model_id must be a valid model identifier (e.g. h3-vdn, wan3.0-video).",
        )
    if not isinstance(params, dict):
        raise AiChiliError("INVALID_PARAMS", "params must be a non-null JSON object.")
    return mode, model_id.strip(), params


def _merge_params(
    params: dict[str, Any] | None,
    *,
    prompt: str | None = None,
    duration_s: int | None = None,
    resolution: str | None = None,
    ratio: str | None = None,
    ref_asset_ids: list[str] | None = None,
    ref_roles: list[str] | None = None,
    seed: int | None = None,
) -> dict[str, Any] | None:
    has_convenience = any(
        val is not None
        for val in (prompt, duration_s, resolution, ratio, ref_asset_ids, ref_roles, seed)
    )
    if params is None:
        if not has_convenience:
            return None
        merged: dict[str, Any] = {}
    elif isinstance(params, dict):
        merged = dict(params)
    else:
        return params  # type: ignore[return-value]

    if prompt is not None:
        merged["prompt"] = prompt
    if duration_s is not None:
        if isinstance(duration_s, bool) or not isinstance(duration_s, int):
            raise AiChiliError("INVALID_PARAMS", "duration_s must be an integer.")
        merged["duration_s"] = duration_s
    if resolution is not None:
        merged["resolution"] = resolution
    if ratio is not None:
        merged["ratio"] = ratio
    if seed is not None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise AiChiliError("INVALID_PARAMS", "seed must be an integer.")
        merged["seed"] = seed
    if ref_asset_ids is not None and len(ref_asset_ids) > 0:
        merged["ref_asset_ids"] = list(ref_asset_ids)
    if ref_roles is not None and len(ref_roles) > 0:
        merged["ref_roles"] = list(ref_roles)
    return merged


def _build_generation_body(
    *,
    mode: str,
    model_id: str,
    params: dict[str, Any] | None,
    workflow: str | None = None,
    storyboard: list[dict[str, Any]] | None = None,
    optimize_prompt: bool | None = None,
    auto_reference: bool | None = None,
    upscale_4k: bool | None = None,
    likeness_consent: bool | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    valid_mode, valid_model_id, valid_params = _validate_generation_request(mode, model_id, params)
    body: dict[str, Any] = {
        "mode": valid_mode,
        "model_id": valid_model_id,
        "params": valid_params,
    }
    if workflow is not None:
        body["workflow"] = workflow
    if storyboard is not None:
        body["storyboard"] = storyboard
    if optimize_prompt is not None:
        body["optimize_prompt"] = bool(optimize_prompt)
    if auto_reference is not None:
        body["auto_reference"] = bool(auto_reference)
    if upscale_4k is not None:
        body["upscale_4k"] = bool(upscale_4k)
    if likeness_consent is not None:
        body["likeness_consent"] = bool(likeness_consent)
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str):
            raise AiChiliError(
                "INVALID_IDEMPOTENCY_KEY",
                "idempotency_key must be a non-empty string between 1 and 128 characters without control characters.",
            )
        cleaned_idem = idempotency_key.strip()
        if (
            not cleaned_idem
            or len(cleaned_idem) > 128
            or _CONTROL_CHAR_RE.search(idempotency_key) is not None
        ):
            raise AiChiliError(
                "INVALID_IDEMPOTENCY_KEY",
                "idempotency_key must be a non-empty string between 1 and 128 characters without control characters.",
            )
        body["idempotency_key"] = cleaned_idem
    return body


def get_platform_overview() -> dict[str, Any]:
    """Return canonical AI Chili Studio & API and OnlyChili Cinema metadata."""
    return {
        "platform": "AI Chili (AIChili) Studio & API",
        "version": VERSION,
        "mcpName": "io.github.AI-Chili/aichili",
        "endpoints": {
            "website": "https://aichili.ai",
            "developers": "https://aichili.ai/developers",
            "openApiSpec": "https://aichili.ai/openapi.json",
            "liveApiOpenApiSpec": "https://videoapi.aichili.ai/v1/openapi.json",
            "remoteMcp": "https://aichili.ai/mcp",
            "pricingDoc": "https://aichili.ai/pricing.md",
            "videoApiBase": DEFAULT_API_BASE,
            "onlyChiliWebsite": ONLYCHILI_CINEMA["url"],
            "onlyChiliMcp": ONLYCHILI_CINEMA["mcpUrl"],
        },
        "freeSignupCredits": FREE_SIGNUP_CREDITS,
        "creditPacks": copy.deepcopy(list(CREDIT_PACKS)),
        "models": copy.deepcopy(list(PUBLIC_MODELS)),
        "onlyChiliCinema": copy.deepcopy(ONLYCHILI_CINEMA),
    }


def estimate_cost(
    seconds: float | int = 15,
    resolution: str = "768p",
    model_id: str = "h3-vdn",
    workflow: str = "talk",
    script_units: float | int | None = None,
    free_signup_credits_used: bool = False,
    subscribers: float | int = 0,
    sub_price: float | int = 9.99,
    ppv_count: float | int = 0,
    ppv_price: float | int = 4.99,
) -> dict[str, Any]:
    """Deterministically calculate PAYG credits, O(1) optimal pack mix, and OnlyChili 80% creator net revenue."""
    if model_id is None or model_id == "":
        normalized_model = "h3-vdn"
    elif isinstance(model_id, str):
        normalized_model = model_id.strip().lower() or "h3-vdn"
    else:
        raise AiChiliError(
            "INVALID_MODEL_ID",
            f"Unsupported local estimation model '{model_id}'. Supported offline models: h3-vdn (h3-sol), avatar-talk.",
        )

    if normalized_model == "avatar-talk":
        if workflow is None or workflow == "":
            normalized_wf = "talk"
        elif isinstance(workflow, str):
            normalized_wf = workflow.strip().lower() or "talk"
        else:
            raise AiChiliError("INVALID_WORKFLOW", "avatar-talk workflow must be 'talk' or 'photo'.")

        if normalized_wf not in ("talk", "photo"):
            raise AiChiliError("INVALID_WORKFLOW", "avatar-talk workflow must be 'talk' or 'photo'.")

        if script_units is not None:
            units = _to_finite_number(
                script_units,
                "INVALID_SCRIPT_UNITS",
                "scriptUnits (or seconds) must be a finite number between 1 and 1,000,000.",
            )
        else:
            dur_for_units = _to_finite_number(
                seconds,
                "INVALID_DURATION",
                "seconds must be a finite number between 1 and 86,400.",
            )
            if dur_for_units <= 0 or dur_for_units > 86_400:
                raise AiChiliError(
                    "INVALID_DURATION",
                    "seconds must be a finite number between 1 and 86,400.",
                )
            units = dur_for_units * 3.8

        if units <= 0 or units > 1_000_000:
            raise AiChiliError(
                "INVALID_SCRIPT_UNITS",
                "scriptUnits (or seconds) must be a finite number between 1 and 1,000,000.",
            )

        blocks = math.ceil(units / AVATAR_TALK_PRICING["blockUnits"])
        base_clip_credits = (
            AVATAR_TALK_PRICING["photoBaseClipCredits"] if normalized_wf == "photo" else 0
        )
        total_credits = math.ceil(blocks * AVATAR_TALK_PRICING["creditsPerBlock"] + base_clip_credits)
        rate_basis: dict[str, Any] = {
            "modelId": "avatar-talk",
            "workflow": normalized_wf,
            "scriptUnits": _clean_num(units),
            "blocks": blocks,
            "creditsPerBlock": AVATAR_TALK_PRICING["creditsPerBlock"],
            "blockUnits": AVATAR_TALK_PRICING["blockUnits"],
            "baseClipCredits": base_clip_credits,
        }
    elif normalized_model in ("h3-vdn", "h3-sol"):
        dur = _to_finite_number(
            seconds,
            "INVALID_DURATION",
            "seconds must be a finite number between 1 and 86,400.",
        )
        if dur <= 0 or dur > 86_400:
            raise AiChiliError(
                "INVALID_DURATION",
                "seconds must be a finite number between 1 and 86,400.",
            )
        if resolution is None or resolution == "":
            normalized_res = "768p"
        elif isinstance(resolution, str):
            normalized_res = resolution.strip().lower() or "768p"
        else:
            raise AiChiliError(
                "INVALID_RESOLUTION",
                f"Unsupported resolution '{resolution}'. Expected one of: {', '.join(H3_VDN_RATES_PER_SECOND)}.",
            )

        rate_per_second = H3_VDN_RATES_PER_SECOND.get(normalized_res)
        if rate_per_second is None:
            raise AiChiliError(
                "INVALID_RESOLUTION",
                f"Unsupported resolution '{resolution}'. Expected one of: {', '.join(H3_VDN_RATES_PER_SECOND)}.",
            )
        total_credits = math.ceil(dur * rate_per_second)
        rate_basis = {
            "modelId": normalized_model,
            "seconds": _clean_num(dur),
            "resolution": normalized_res,
            "creditsPerSecond": _clean_num(rate_per_second),
        }
    elif normalized_model in VENDOR_QUOTED_MODELS:
        raise AiChiliError(
            "VENDOR_QUOTED_MODEL",
            f"Model '{normalized_model}' uses live vendor quotes rather than fixed per-second rates. Call plan_generation / POST /v1/generations/plan (or MCP tool plan_video_generation) for an exact $0 dry-run quote.",
        )
    else:
        raise AiChiliError(
            "INVALID_MODEL_ID",
            f"Unsupported local estimation model '{model_id}'. Supported offline models: h3-vdn (h3-sol), avatar-talk.",
        )

    subs = _to_finite_number(
        subscribers, "INVALID_SUBSCRIBERS", "subscribers must be a non-negative number."
    )
    if subs < 0:
        raise AiChiliError("INVALID_SUBSCRIBERS", "subscribers must be a non-negative number.")

    ppvs = _to_finite_number(
        ppv_count, "INVALID_PPV_COUNT", "ppvCount must be a non-negative number."
    )
    if ppvs < 0:
        raise AiChiliError("INVALID_PPV_COUNT", "ppvCount must be a non-negative number.")

    sub_min = ONLYCHILI_CINEMA["membershipPriceRangeUsd"]["min"]
    sub_max = ONLYCHILI_CINEMA["membershipPriceRangeUsd"]["max"]
    sub_usd = 0.0
    if subs > 0:
        sub_usd = _to_finite_number(
            sub_price,
            "INVALID_SUB_PRICE",
            f"OnlyChili membership price must be between ${sub_min} and ${sub_max}/mo.",
        )
        if sub_usd < sub_min or sub_usd > sub_max:
            raise AiChiliError(
                "INVALID_SUB_PRICE",
                f"OnlyChili membership price must be between ${sub_min} and ${sub_max}/mo.",
            )

    ppv_min = ONLYCHILI_CINEMA["ppvPriceRangeUsd"]["min"]
    ppv_max = ONLYCHILI_CINEMA["ppvPriceRangeUsd"]["max"]
    ppv_usd = 0.0
    if ppvs > 0:
        ppv_usd = _to_finite_number(
            ppv_price,
            "INVALID_PPV_PRICE",
            f"OnlyChili PPV price must be between ${ppv_min} and ${ppv_max}.",
        )
        if ppv_usd < ppv_min or ppv_usd > ppv_max:
            raise AiChiliError(
                "INVALID_PPV_PRICE",
                f"OnlyChili PPV price must be between ${ppv_min} and ${ppv_max}.",
            )

    used_free = bool(free_signup_credits_used)
    free_credits_applied = 0 if used_free else min(total_credits, FREE_SIGNUP_CREDITS)
    billable_credits = _round_js(max(0, total_credits - free_credits_applied), 3)

    paid_packs = [p for p in CREDIT_PACKS if p["priceUsd"] > 0]
    pack_breakdown = []
    for pack in paid_packs:
        usd_per_credit = pack["priceUsd"] / pack["credits"]
        packs_needed = 0 if billable_credits == 0 else math.ceil(billable_credits / pack["credits"])
        pack_breakdown.append(
            {
                "id": pack["id"],
                "name": pack["name"],
                "packPriceUsd": _clean_num(pack["priceUsd"]),
                "packCredits": pack["credits"],
                "usdPerCredit": _round_js(usd_per_credit, 4),
                "effectiveCostUsd": _round_js(billable_credits * usd_per_credit, 2),
                "packsNeededAlone": packs_needed,
                "checkoutSpendUsd": _round_js(packs_needed * pack["priceUsd"], 2),
            }
        )

    # Exact O(1) 24-step mixed-pack solver across Studio & API (780 cr), Creator (460 cr), Starter (150 cr).
    if billable_credits == 0:
        recommended_pack: dict[str, Any] = {
            "id": "free-signup",
            "name": "Free Sign-Up",
            "checkoutSpendUsd": 0,
            "creditsIncluded": FREE_SIGNUP_CREDITS,
        }
    else:
        best_cost = math.inf
        best_credits = 0
        best_s = 0
        best_c = 0
        best_st = 0
        for c in range(4):
            for st in range(6):
                rem = max(0, billable_credits - c * 460 - st * 150)
                s = math.ceil(rem / 780)
                credits = s * 780 + c * 460 + st * 150
                cost = float(_round_js(s * 49.9 + c * 29.9 + st * 9.9, 2))
                if cost < best_cost or (cost == best_cost and credits > best_credits):
                    best_cost = cost
                    best_credits = credits
                    best_s = s
                    best_c = c
                    best_st = st

        non_zero = [
            item
            for item in (
                {"id": "studio-api", "name": "Studio & API", "count": best_s} if best_s > 0 else None,
                {"id": "creator", "name": "Creator", "count": best_c} if best_c > 0 else None,
                {"id": "starter", "name": "Starter", "count": best_st} if best_st > 0 else None,
            )
            if item is not None
        ]
        clean_best_cost = _clean_num(best_cost)
        if len(non_zero) == 1 and non_zero[0]["count"] == 1:
            recommended_pack = {
                "id": non_zero[0]["id"],
                "name": non_zero[0]["name"],
                "checkoutSpendUsd": clean_best_cost,
                "creditsIncluded": best_credits,
            }
        else:
            recommended_pack = {
                "id": non_zero[0]["id"] if len(non_zero) == 1 else "mixed",
                "name": " + ".join(f"{p['count']}x {p['name']}" for p in non_zero),
                "checkoutSpendUsd": clean_best_cost,
                "creditsIncluded": best_credits,
            }

    studio_pack = paid_packs[-1]
    effective_usd_at_studio_rate = _round_js(
        billable_credits * (studio_pack["priceUsd"] / studio_pack["credits"]), 2
    )

    gross_membership_usd = _round_js(subs * sub_usd, 2) if subs > 0 else 0
    gross_ppv_usd = _round_js(ppvs * ppv_usd, 2) if ppvs > 0 else 0
    gross_revenue_usd = _round_js(gross_membership_usd + gross_ppv_usd, 2)
    creator_net_revenue_usd = _round_js(
        gross_revenue_usd * (ONLYCHILI_CINEMA["creatorNetRevenueSharePct"] / 100),
        2,
    )

    return {
        "rateBasis": rate_basis,
        "totalCredits": total_credits,
        "freeSignupCreditsAvailable": 0 if used_free else FREE_SIGNUP_CREDITS,
        "freeCreditsApplied": free_credits_applied,
        "billableCredits": billable_credits,
        "effectiveUsdAtStudioRate": effective_usd_at_studio_rate,
        "recommendedPack": recommended_pack,
        "packBreakdown": pack_breakdown,
        "creditPacks": copy.deepcopy(list(CREDIT_PACKS)),
        "onlyChiliProjection": {
            "creatorNetRevenueSharePct": ONLYCHILI_CINEMA["creatorNetRevenueSharePct"],
            "languagesSupported": ONLYCHILI_CINEMA["languagesSupported"],
            "subscribers": _clean_num(subs),
            "subPriceUsd": _clean_num(sub_usd) if subs > 0 else 0,
            "ppvCount": _clean_num(ppvs),
            "ppvPriceUsd": _clean_num(ppv_usd) if ppvs > 0 else 0,
            "grossRevenueUsd": gross_revenue_usd,
            "creatorNetRevenueUsd": creator_net_revenue_usd,
            "netAfterStudioVideoCostUsd": _round_js(
                creator_net_revenue_usd - effective_usd_at_studio_rate, 2
            ),
        },
    }


class AiChiliClient:
    """Zero-dependency synchronous client for the AI Chili Video Generation Studio & API."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        transport: TransportCallable | None = None,
    ) -> None:
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise AiChiliError("INVALID_TIMEOUT", "timeout_s must be a positive finite number.")
        self._api_key = api_key
        self._base_url = base_url
        self._timeout_s = float(timeout_s)
        self._transport: TransportCallable = transport or _default_transport

    @property
    def base_url(self) -> str:
        return resolve_base_url(self._base_url)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        require_auth: bool = False,
        body: dict[str, Any] | None = None,
    ) -> Any:
        resolved_base = resolve_base_url(self._base_url)
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if require_auth or self._api_key is not None:
            valid_key = validate_api_key(self._api_key)
            headers["Authorization"] = f"Bearer {valid_key}"

        body_bytes: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(body, allow_nan=False).encode("utf-8")

        url = f"{resolved_base}{path}"
        status, raw_bytes = self._transport(method, url, headers, body_bytes, self._timeout_s)

        raw_text = raw_bytes.decode("utf-8", errors="replace") if raw_bytes else ""
        parsed: Any = None
        if raw_text:
            try:
                parsed = json.loads(raw_text)
            except ValueError:
                parsed = {"raw": raw_text}

        if status < 200 or status >= 300:
            envelope: Any = None
            if isinstance(parsed, dict):
                envelope = (
                    parsed.get("detail")
                    if parsed.get("detail") is not None
                    else parsed.get("error")
                )

            if isinstance(envelope, dict) and isinstance(envelope.get("code"), str):
                code = envelope["code"]
            else:
                code = f"HTTP_{status}"

            if isinstance(envelope, dict) and isinstance(envelope.get("message"), str):
                message = envelope["message"]
            elif isinstance(envelope, str):
                message = envelope
            else:
                message = f"AI Chili API returned HTTP {status}"

            resolution_hint = (
                envelope.get("resolution_hint")
                if isinstance(envelope, dict) and isinstance(envelope.get("resolution_hint"), str)
                else None
            )

            raise AiChiliError(
                code,
                message,
                status=status,
                details=parsed,
                resolution_hint=resolution_hint,
            )

        return parsed

    def platform_overview(self) -> dict[str, Any]:
        return get_platform_overview()

    def estimate_cost(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return estimate_cost(*args, **kwargs)

    def list_models(self, scope: str | None = None) -> dict[str, Any]:
        query = ""
        if scope is not None and scope != "":
            if not isinstance(scope, str) or scope not in VALID_SCOPES:
                raise AiChiliError(
                    "INVALID_SCOPE",
                    f"Invalid scope '{scope}'. Expected 'available' or 'console'.",
                )
            query = f"?scope={urllib.parse.quote(scope, safe='')}"
        return self._request_json("GET", f"/v1/registry{query}", require_auth=False)

    def get_credits(self) -> dict[str, Any]:
        validate_api_key(self._api_key)
        return self._request_json("GET", "/v1/credits", require_auth=True)

    def plan_generation(
        self,
        mode: str,
        model_id: str,
        params: dict[str, Any] | None = None,
        *,
        prompt: str | None = None,
        duration_s: int | None = None,
        resolution: str | None = None,
        ratio: str | None = None,
        ref_asset_ids: list[str] | None = None,
        ref_roles: list[str] | None = None,
        seed: int | None = None,
        workflow: str | None = None,
        storyboard: list[dict[str, Any]] | None = None,
        optimize_prompt: bool | None = None,
        auto_reference: bool | None = None,
        upscale_4k: bool | None = None,
        likeness_consent: bool | None = None,
    ) -> dict[str, Any]:
        validate_api_key(self._api_key)
        merged_params = _merge_params(
            params,
            prompt=prompt,
            duration_s=duration_s,
            resolution=resolution,
            ratio=ratio,
            ref_asset_ids=ref_asset_ids,
            ref_roles=ref_roles,
            seed=seed,
        )
        body = _build_generation_body(
            mode=mode,
            model_id=model_id,
            params=merged_params,
            workflow=workflow,
            storyboard=storyboard,
            optimize_prompt=optimize_prompt,
            auto_reference=auto_reference,
            upscale_4k=upscale_4k,
            likeness_consent=likeness_consent,
        )
        return self._request_json("POST", "/v1/generations/plan", require_auth=True, body=body)

    def create_generation(
        self,
        mode: str,
        model_id: str,
        params: dict[str, Any] | None = None,
        *,
        prompt: str | None = None,
        duration_s: int | None = None,
        resolution: str | None = None,
        ratio: str | None = None,
        ref_asset_ids: list[str] | None = None,
        ref_roles: list[str] | None = None,
        seed: int | None = None,
        workflow: str | None = None,
        storyboard: list[dict[str, Any]] | None = None,
        optimize_prompt: bool | None = None,
        auto_reference: bool | None = None,
        upscale_4k: bool | None = None,
        likeness_consent: bool | None = None,
        idempotency_key: str | None = None,
        auto_idempotency: bool = False,
    ) -> dict[str, Any]:
        validate_api_key(self._api_key)
        effective_idem = (
            idempotency_key
            if idempotency_key is not None
            else (str(uuid.uuid4()) if auto_idempotency else None)
        )
        merged_params = _merge_params(
            params,
            prompt=prompt,
            duration_s=duration_s,
            resolution=resolution,
            ratio=ratio,
            ref_asset_ids=ref_asset_ids,
            ref_roles=ref_roles,
            seed=seed,
        )
        body = _build_generation_body(
            mode=mode,
            model_id=model_id,
            params=merged_params,
            workflow=workflow,
            storyboard=storyboard,
            optimize_prompt=optimize_prompt,
            auto_reference=auto_reference,
            upscale_4k=upscale_4k,
            likeness_consent=likeness_consent,
            idempotency_key=effective_idem,
        )
        return self._request_json("POST", "/v1/generations", require_auth=True, body=body)

    def get_generation(self, job_id: str) -> dict[str, Any]:
        validate_api_key(self._api_key)
        valid_job_id = validate_job_id(job_id)
        encoded = urllib.parse.quote(valid_job_id, safe="")
        return self._request_json("GET", f"/v1/generations/{encoded}", require_auth=True)

    def wait_for_generation(
        self,
        job_id: str,
        poll_interval_s: float = 5.0,
        timeout_s: float = 600.0,
    ) -> dict[str, Any]:
        if isinstance(poll_interval_s, bool) or not isinstance(poll_interval_s, (int, float)) or not math.isfinite(poll_interval_s) or poll_interval_s <= 0:
            raise AiChiliError("INVALID_POLL_INTERVAL", "poll_interval_s must be a positive finite number.")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise AiChiliError("INVALID_TIMEOUT", "timeout_s must be a positive finite number.")

        deadline = time.monotonic() + float(timeout_s)
        while True:
            job = self.get_generation(job_id)
            status_val = job.get("status") if isinstance(job, dict) else None
            if isinstance(status_val, str) and status_val.lower() in TERMINAL_STATUSES:
                return job
            if time.monotonic() + float(poll_interval_s) > deadline:
                last_status = status_val if isinstance(status_val, str) else "unknown"
                raise AiChiliError(
                    "POLL_TIMEOUT",
                    f"Timed out after {timeout_s}s waiting for generation {job_id} (last status: {last_status}).",
                    details=job,
                )
            time.sleep(float(poll_interval_s))


def list_models(
    *,
    scope: str | None = None,
    base_url: str | None = None,
    timeout_s: float = 30.0,
    transport: TransportCallable | None = None,
) -> dict[str, Any]:
    return AiChiliClient(base_url=base_url, timeout_s=timeout_s, transport=transport).list_models(
        scope=scope
    )


def get_credits(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_s: float = 30.0,
    transport: TransportCallable | None = None,
) -> dict[str, Any]:
    return AiChiliClient(
        api_key=api_key, base_url=base_url, timeout_s=timeout_s, transport=transport
    ).get_credits()


def plan_generation(
    *,
    mode: str,
    model_id: str,
    params: dict[str, Any] | None = None,
    prompt: str | None = None,
    duration_s: int | None = None,
    resolution: str | None = None,
    ratio: str | None = None,
    ref_asset_ids: list[str] | None = None,
    ref_roles: list[str] | None = None,
    seed: int | None = None,
    workflow: str | None = None,
    storyboard: list[dict[str, Any]] | None = None,
    optimize_prompt: bool | None = None,
    auto_reference: bool | None = None,
    upscale_4k: bool | None = None,
    likeness_consent: bool | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_s: float = 30.0,
    transport: TransportCallable | None = None,
) -> dict[str, Any]:
    return AiChiliClient(
        api_key=api_key, base_url=base_url, timeout_s=timeout_s, transport=transport
    ).plan_generation(
        mode=mode,
        model_id=model_id,
        params=params,
        prompt=prompt,
        duration_s=duration_s,
        resolution=resolution,
        ratio=ratio,
        ref_asset_ids=ref_asset_ids,
        ref_roles=ref_roles,
        seed=seed,
        workflow=workflow,
        storyboard=storyboard,
        optimize_prompt=optimize_prompt,
        auto_reference=auto_reference,
        upscale_4k=upscale_4k,
        likeness_consent=likeness_consent,
    )


def create_generation(
    *,
    mode: str,
    model_id: str,
    params: dict[str, Any] | None = None,
    prompt: str | None = None,
    duration_s: int | None = None,
    resolution: str | None = None,
    ratio: str | None = None,
    ref_asset_ids: list[str] | None = None,
    ref_roles: list[str] | None = None,
    seed: int | None = None,
    workflow: str | None = None,
    storyboard: list[dict[str, Any]] | None = None,
    optimize_prompt: bool | None = None,
    auto_reference: bool | None = None,
    upscale_4k: bool | None = None,
    likeness_consent: bool | None = None,
    idempotency_key: str | None = None,
    auto_idempotency: bool = False,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_s: float = 30.0,
    transport: TransportCallable | None = None,
) -> dict[str, Any]:
    return AiChiliClient(
        api_key=api_key, base_url=base_url, timeout_s=timeout_s, transport=transport
    ).create_generation(
        mode=mode,
        model_id=model_id,
        params=params,
        prompt=prompt,
        duration_s=duration_s,
        resolution=resolution,
        ratio=ratio,
        ref_asset_ids=ref_asset_ids,
        ref_roles=ref_roles,
        seed=seed,
        workflow=workflow,
        storyboard=storyboard,
        optimize_prompt=optimize_prompt,
        auto_reference=auto_reference,
        upscale_4k=upscale_4k,
        likeness_consent=likeness_consent,
        idempotency_key=idempotency_key,
        auto_idempotency=auto_idempotency,
    )


def get_generation(
    job_id: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_s: float = 30.0,
    transport: TransportCallable | None = None,
) -> dict[str, Any]:
    return AiChiliClient(
        api_key=api_key, base_url=base_url, timeout_s=timeout_s, transport=transport
    ).get_generation(job_id)


def wait_for_generation(
    job_id: str,
    *,
    poll_interval_s: float = 5.0,
    timeout_s: float = 600.0,
    api_key: str | None = None,
    base_url: str | None = None,
    request_timeout_s: float = 30.0,
    transport: TransportCallable | None = None,
) -> dict[str, Any]:
    return AiChiliClient(
        api_key=api_key, base_url=base_url, timeout_s=request_timeout_s, transport=transport
    ).wait_for_generation(job_id, poll_interval_s=poll_interval_s, timeout_s=timeout_s)
