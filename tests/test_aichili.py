"""Comprehensive stdlib unittest suite for the zero-dependency `aichili` SDK, CLI, and MCP server."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from aichili import (  # noqa: E402
    AVATAR_TALK_PRICING,
    CREDIT_PACKS,
    FREE_SIGNUP_CREDITS,
    H3_VDN_RATES_PER_SECOND,
    ONLYCHILI_CINEMA,
    PUBLIC_MODELS,
    USER_AGENT,
    VERSION,
    AiChiliClient,
    AiChiliError,
    estimate_cost,
    get_platform_overview,
    list_models,
    resolve_base_url,
    validate_api_key,
    validate_job_id,
)
from aichili.mcp import (  # noqa: E402
    DEFAULT_PROTOCOL_VERSION,
    MAX_STDIO_LINE_BYTES,
    handle_jsonrpc_message,
    start_mcp_stdio_server,
)


class TestPricingAndParity(unittest.TestCase):
    def test_ssot_constants_and_payg_pack_math_parity(self) -> None:
        self.assertEqual(VERSION, "1.0.1")
        self.assertEqual(USER_AGENT, "aichili-python/1.0.1 (+https://aichili.ai/developers)")
        self.assertEqual(FREE_SIGNUP_CREDITS, 25)
        self.assertEqual(
            [[p["id"], p["priceUsd"], p["credits"]] for p in CREDIT_PACKS],
            [
                ["free-signup", 0, 25],
                ["starter", 9.9, 150],
                ["creator", 29.9, 460],
                ["studio-api", 49.9, 780],
            ],
        )
        self.assertEqual(
            H3_VDN_RATES_PER_SECOND,
            {"480p": 0.6, "768p": 1.0, "1080p": 1.8, "2k": 2.5},
        )
        self.assertEqual(ONLYCHILI_CINEMA["creatorNetRevenueSharePct"], 80)
        self.assertEqual(ONLYCHILI_CINEMA["languagesSupported"], 47)
        self.assertEqual(AVATAR_TALK_PRICING["photoBaseClipCredits"], 10)

        vdn = estimate_cost(
            seconds=90,
            resolution="768p",
            free_signup_credits_used=False,
            subscribers=10,
            sub_price=9.99,
            ppv_count=5,
            ppv_price=4.99,
        )
        self.assertEqual(vdn["totalCredits"], 90)
        self.assertEqual(vdn["freeCreditsApplied"], 25)
        self.assertEqual(vdn["billableCredits"], 65)
        self.assertEqual(vdn["recommendedPack"]["id"], "starter")
        self.assertEqual(vdn["onlyChiliProjection"]["grossRevenueUsd"], 124.85)
        self.assertEqual(vdn["onlyChiliProjection"]["creatorNetRevenueUsd"], 99.88)
        self.assertEqual(vdn["effectiveUsdAtStudioRate"], 4.16)
        self.assertEqual(vdn["onlyChiliProjection"]["netAfterStudioVideoCostUsd"], 95.72)

        # Whole-credit ceiling parity with services/control (5s @ 2k = 12.5 -> 13; 7s @ 480p = 4.2 -> 5)
        self.assertEqual(
            estimate_cost(seconds=5, resolution="2k", free_signup_credits_used=True)["totalCredits"],
            13,
        )
        self.assertEqual(
            estimate_cost(seconds=7, resolution="480p", free_signup_credits_used=True)["totalCredits"],
            5,
        )

        # Free sign-up covers <= 25 credits completely
        free_fit = estimate_cost(seconds=15, resolution="768p", free_signup_credits_used=False)
        self.assertEqual(free_fit["billableCredits"], 0)
        self.assertEqual(free_fit["recommendedPack"]["id"], "free-signup")
        self.assertEqual(free_fit["recommendedPack"]["checkoutSpendUsd"], 0)

    def test_h3_sol_alias_and_vendor_quoted_model_guards(self) -> None:
        h3_sol = estimate_cost(
            model_id="h3-sol",
            seconds=10,
            resolution="768p",
            free_signup_credits_used=True,
        )
        self.assertEqual(h3_sol["totalCredits"], 10)
        self.assertEqual(h3_sol["rateBasis"]["modelId"], "h3-sol")

        for vendor_model in (
            "wan3.0-video",
            "wan-3",
            "seedance-2.5",
            "seedance-2-5",
            "minimax-h3-gmi",
            "minimax-h3",
        ):
            with self.assertRaises(AiChiliError) as ctx:
                estimate_cost(model_id=vendor_model, seconds=10)
            self.assertEqual(ctx.exception.code, "VENDOR_QUOTED_MODEL")

        with self.assertRaises(AiChiliError) as unknown_ctx:
            estimate_cost(model_id="nonexistent-model", seconds=10)
        self.assertEqual(unknown_ctx.exception.code, "INVALID_MODEL_ID")

    def test_avatar_talk_credits_and_o1_pack_solver_and_bounds(self) -> None:
        av_units = estimate_cost(
            model_id="avatar-talk",
            script_units=23,
            free_signup_credits_used=True,
        )
        self.assertEqual(av_units["totalCredits"], 1)
        self.assertEqual(av_units["billableCredits"], 1)

        talk_25 = estimate_cost(
            model_id="avatar-talk",
            workflow="talk",
            script_units=25,
            free_signup_credits_used=True,
        )
        self.assertEqual(talk_25["totalCredits"], 1)

        photo_25 = estimate_cost(
            model_id="avatar-talk",
            workflow="photo",
            script_units=25,
            free_signup_credits_used=True,
        )
        self.assertEqual(photo_25["totalCredits"], 11)

        seconds_fallback = estimate_cost(
            model_id="avatar-talk",
            seconds=10,
            free_signup_credits_used=True,
        )
        self.assertEqual(seconds_fallback["rateBasis"]["scriptUnits"], 38)
        self.assertEqual(seconds_fallback["totalCredits"], 2)

        # Bounded O(1) 24-step mixed-pack optimizer
        mixed_925 = estimate_cost(seconds=925, resolution="768p", free_signup_credits_used=True)
        self.assertEqual(mixed_925["recommendedPack"]["checkoutSpendUsd"], 59.8)
        self.assertEqual(mixed_925["recommendedPack"]["creditsIncluded"], 930)
        self.assertEqual(mixed_925["recommendedPack"]["id"], "mixed")

        mixed_515 = estimate_cost(seconds=540, resolution="768p", free_signup_credits_used=False)
        self.assertEqual(mixed_515["billableCredits"], 515)
        self.assertEqual(mixed_515["recommendedPack"]["checkoutSpendUsd"], 39.6)

        max_bounded = estimate_cost(seconds=86_400, resolution="2k", model_id="h3-sol")
        self.assertEqual(max_bounded["totalCredits"], 216_000)

        # Input upper bounds & invalid types (including Python bool subclassing int!)
        for bad_seconds in (0, -5, 86_401, 1e12, float("inf"), float("nan"), "abc", True):
            with self.assertRaises(AiChiliError) as ctx:
                estimate_cost(seconds=bad_seconds)  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.code, "INVALID_DURATION")

        for bad_units in (0, -1, 1_000_001, 2_000_000, True):
            with self.assertRaises(AiChiliError) as ctx:
                estimate_cost(model_id="avatar-talk", workflow="talk", script_units=bad_units)
            self.assertEqual(ctx.exception.code, "INVALID_SCRIPT_UNITS")

        with self.assertRaises(AiChiliError) as wf_ctx:
            estimate_cost(model_id="avatar-talk", workflow="invalid-wf")
        self.assertEqual(wf_ctx.exception.code, "INVALID_WORKFLOW")

        with self.assertRaises(AiChiliError) as res_ctx:
            estimate_cost(seconds=15, resolution="8k")
        self.assertEqual(res_ctx.exception.code, "INVALID_RESOLUTION")

        with self.assertRaises(AiChiliError) as sub_ctx:
            estimate_cost(seconds=15, subscribers=5, sub_price=1.5)
        self.assertEqual(sub_ctx.exception.code, "INVALID_SUB_PRICE")

        with self.assertRaises(AiChiliError) as ppv_ctx:
            estimate_cost(seconds=15, ppv_count=2, ppv_price=600.0)
        self.assertEqual(ppv_ctx.exception.code, "INVALID_PPV_PRICE")

        # Mutation isolation check
        overview = get_platform_overview()
        overview["creditPacks"][0]["credits"] = 999999
        self.assertEqual(CREDIT_PACKS[0]["credits"], 25)


class TestClientSecurityAndPolling(unittest.TestCase):
    def test_rejects_bad_keys_traversal_job_ids_and_insecure_urls_before_network(self) -> None:
        network_calls = 0
        captured_requests: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []

        def mock_transport(
            method: str,
            url: str,
            headers: dict[str, str],
            body_bytes: bytes | None,
            timeout_s: float,
        ) -> tuple[int, bytes]:
            nonlocal network_calls
            network_calls += 1
            decoded_body = json.loads(body_bytes.decode("utf-8")) if body_bytes else None
            captured_requests.append((method, url, headers, decoded_body))
            payload = {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "mode": "t2v",
                "model_id": "h3-vdn",
                "status": "queued",
                "params": {"prompt": "test"},
                "credits_cost": 15,
            }
            return 200, json.dumps(payload).encode("utf-8")

        bad_keys = [
            "",
            "sk-not-chili-123456789",
            "chili_sk_short",
            "chili_sk_has space1234",
            "chili_sk_12345678\nEvil: 1",
        ]
        for bad_key in bad_keys:
            client = AiChiliClient(api_key=bad_key, transport=mock_transport)
            with self.assertRaises(AiChiliError) as k_ctx:
                client.get_credits()
            self.assertEqual(k_ctx.exception.code, "INVALID_API_KEY")
            # Ensure malformed key material is never echoed in error message
            if bad_key.strip():
                self.assertNotIn(bad_key.strip(), k_ctx.exception.message)

            with self.assertRaises(AiChiliError) as p_ctx:
                client.plan_generation("t2v", "h3-vdn", {"prompt": "hi", "duration_s": 5})
            self.assertEqual(p_ctx.exception.code, "INVALID_API_KEY")

        valid_client = AiChiliClient(
            api_key="chili_sk_validkey123456",
            base_url="https://videoapi.aichili.ai/",
            transport=mock_transport,
        )
        bad_job_ids = [
            "",
            "-rf",
            "_leading_underscore",
            "..",
            "../credits",
            "../../etc/passwd",
            "550e8400/cancel",
            "job..traversal",
            "job%2fcredits",
            "job%00null",
            "job?admin=true",
            "job#fragment",
            " job_with_leading_space",
            "job_with_trailing_newline\n",
            "job with space",
            "a" * 129,
        ]
        for bad_job_id in bad_job_ids:
            with self.assertRaises(AiChiliError) as j_ctx:
                valid_client.get_generation(bad_job_id)
            self.assertEqual(j_ctx.exception.code, "INVALID_JOB_ID")
            with self.assertRaises(AiChiliError):
                validate_job_id(bad_job_id)

        for bad_idem in (
            "",
            "   ",
            "shot-1\r\nX-Injected: 1",
            "shot\x00null",
            "shot-1\n",
            "x" * 129,
        ):
            with self.assertRaises(AiChiliError) as idem_ctx:
                valid_client.create_generation(
                    mode="t2v",
                    model_id="h3-vdn",
                    prompt="sunset ocean",
                    idempotency_key=bad_idem,
                )
            self.assertEqual(idem_ctx.exception.code, "INVALID_IDEMPOTENCY_KEY")

        self.assertEqual(network_calls, 0)

        # Valid calls inspect exact headers, URL normalization, and idempotency_key
        ok_job = valid_client.get_generation("550e8400-e29b-41d4-a716-446655440000")
        self.assertEqual(ok_job["status"], "queued")
        self.assertEqual(network_calls, 1)
        method, url, headers, _ = captured_requests[0]
        self.assertEqual(method, "GET")
        self.assertEqual(
            url, "https://videoapi.aichili.ai/v1/generations/550e8400-e29b-41d4-a716-446655440000"
        )
        self.assertEqual(headers["User-Agent"], USER_AGENT)
        self.assertEqual(headers["Authorization"], "Bearer chili_sk_validkey123456")

        created = valid_client.create_generation(
            mode="i2v",
            model_id="h3-vdn",
            prompt="sunset ocean",
            duration_s=5,
            resolution="768p",
            ratio="16:9",
            ref_asset_ids=["00000000-0000-0000-0000-000000000001"],
            ref_roles=["first_frame"],
            idempotency_key="idem-key-001",
        )
        self.assertEqual(created["status"], "queued")
        self.assertEqual(network_calls, 2)
        _, post_url, _, post_body = captured_requests[1]
        self.assertEqual(post_url, "https://videoapi.aichili.ai/v1/generations")
        assert post_body is not None
        self.assertEqual(post_body["idempotency_key"], "idem-key-001")
        self.assertEqual(
            post_body["params"],
            {
                "prompt": "sunset ocean",
                "duration_s": 5,
                "resolution": "768p",
                "ratio": "16:9",
                "ref_asset_ids": ["00000000-0000-0000-0000-000000000001"],
                "ref_roles": ["first_frame"],
            },
        )

        # Base URL security hardening
        with self.assertRaises(AiChiliError) as http_ctx:
            resolve_base_url("http://videoapi.aichili.ai")
        self.assertEqual(http_ctx.exception.code, "INSECURE_BASE_URL")

        for bad_base in (
            "https://user:pass@videoapi.aichili.ai",
            "https://videoapi.aichili.ai?evil=1",
            "https://videoapi.aichili.ai#frag",
            "ftp://videoapi.aichili.ai",
            "https://videoapi.aichili.ai\r\nEvil: 1",
        ):
            with self.assertRaises(AiChiliError) as b_ctx:
                resolve_base_url(bad_base)
            self.assertEqual(b_ctx.exception.code, "INVALID_BASE_URL")

        self.assertEqual(resolve_base_url("http://127.0.0.1:8000/"), "http://127.0.0.1:8000")
        self.assertEqual(resolve_base_url("http://localhost:8000/"), "http://localhost:8000")
        self.assertEqual(resolve_base_url("http://[::1]:8000/"), "http://[::1]:8000")
        self.assertEqual(validate_api_key("chili_sk_12345678"), "chili_sk_12345678")

        # Verify both `detail` (videoapi.aichili.ai) and `error` (aichili.ai/api edge gateway) error envelopes
        def detail_err_transport(
            method: str,
            url: str,
            headers: dict[str, str],
            body_bytes: bytes | None,
            timeout_s: float,
        ) -> tuple[int, bytes]:
            return 402, json.dumps(
                {"detail": {"code": "insufficient_credits", "message": "Wallet balance is 0"}}
            ).encode("utf-8")

        def gateway_err_transport(
            method: str,
            url: str,
            headers: dict[str, str],
            body_bytes: bytes | None,
            timeout_s: float,
        ) -> tuple[int, bytes]:
            return 429, json.dumps(
                {
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "Burst limit reached",
                        "resolution_hint": "Retry after 2 seconds or upgrade to Studio & API tier.",
                    }
                }
            ).encode("utf-8")

        with self.assertRaises(AiChiliError) as det_ctx:
            AiChiliClient(api_key="chili_sk_validkey123456", transport=detail_err_transport).get_credits()
        self.assertEqual(det_ctx.exception.status, 402)
        self.assertEqual(det_ctx.exception.code, "insufficient_credits")
        self.assertEqual(det_ctx.exception.message, "Wallet balance is 0")

        with self.assertRaises(AiChiliError) as gw_ctx:
            AiChiliClient(api_key="chili_sk_validkey123456", transport=gateway_err_transport).get_credits()
        self.assertEqual(gw_ctx.exception.status, 429)
        self.assertEqual(gw_ctx.exception.code, "rate_limit_exceeded")
        self.assertEqual(gw_ctx.exception.message, "Burst limit reached")
        self.assertEqual(
            gw_ctx.exception.resolution_hint,
            "Retry after 2 seconds or upgrade to Studio & API tier.",
        )

    def test_wait_for_generation_polling_and_timeout(self) -> None:
        statuses = iter(["queued", "running", "succeeded"])

        def poll_transport(
            method: str,
            url: str,
            headers: dict[str, str],
            body_bytes: bytes | None,
            timeout_s: float,
        ) -> tuple[int, bytes]:
            st = next(statuses)
            return 200, json.dumps({"id": "job_123", "status": st}).encode("utf-8")

        client = AiChiliClient(api_key="chili_sk_validkey123456", transport=poll_transport)
        done = client.wait_for_generation("job_123", poll_interval_s=0.005, timeout_s=1.0)
        self.assertEqual(done["status"], "succeeded")

        def stuck_transport(
            method: str,
            url: str,
            headers: dict[str, str],
            body_bytes: bytes | None,
            timeout_s: float,
        ) -> tuple[int, bytes]:
            return 200, json.dumps({"id": "job_stuck", "status": "running"}).encode("utf-8")

        stuck_client = AiChiliClient(api_key="chili_sk_validkey123456", transport=stuck_transport)
        with self.assertRaises(AiChiliError) as t_ctx:
            stuck_client.wait_for_generation("job_stuck", poll_interval_s=0.05, timeout_s=0.02)
        self.assertEqual(t_ctx.exception.code, "POLL_TIMEOUT")


class TestCliAndMcpSubprocesses(unittest.TestCase):
    def _subproc_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["AICHILI_API_KEY"] = ""
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing_pp}" if existing_pp else str(SRC_DIR)
        return env

    def test_cli_subprocess_version_estimate_and_invalid_flag_exit_1(self) -> None:
        env = self._subproc_env()

        ver = subprocess.run(
            [sys.executable, "-m", "aichili.cli", "--version"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(ver.stdout.strip(), VERSION)

        ver_json = subprocess.run(
            [sys.executable, "-m", "aichili.cli", "--version", "--json"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(ver_json.stdout), {"name": "aichili", "version": VERSION})

        help_res = subprocess.run(
            [sys.executable, "-m", "aichili.cli", "--help"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn(f"aichili v{VERSION}", help_res.stdout)

        est = subprocess.run(
            [
                sys.executable,
                "-m",
                "aichili.cli",
                "estimate",
                "--seconds",
                "90",
                "--resolution",
                "768p",
                "--json",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        est_json = json.loads(est.stdout)
        self.assertEqual(est_json["totalCredits"], 90)
        self.assertEqual(est_json["billableCredits"], 65)

        bad_flag = subprocess.run(
            [sys.executable, "-m", "aichili.cli", "--bogus-flag", "--json"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(bad_flag.returncode, 1)
        self.assertEqual(json.loads(bad_flag.stderr)["error"]["code"], "INVALID_ARGS")

        no_key = subprocess.run(
            [sys.executable, "-m", "aichili.cli", "credits", "--json"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(no_key.returncode, 1)
        self.assertEqual(json.loads(no_key.stderr)["error"]["code"], "INVALID_API_KEY")

        zero_sec = subprocess.run(
            [sys.executable, "-m", "aichili.cli", "estimate", "--seconds", "0", "--json"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(zero_sec.returncode, 1)
        self.assertEqual(json.loads(zero_sec.stderr)["error"]["code"], "INVALID_DURATION")

        # Verify CLI `generate` repeatable --ref-asset / --ref-role, --idempotency-key, --wait, and `status <job_id>`
        from aichili.cli import main as cli_main

        cli_calls: list[tuple[str, str, dict[str, object] | None]] = []

        def cli_transport(
            method: str,
            url: str,
            headers: dict[str, str],
            body_bytes: bytes | None,
            timeout_s: float,
        ) -> tuple[int, bytes]:
            parsed_b = json.loads(body_bytes.decode("utf-8")) if body_bytes else None
            cli_calls.append((method, url, parsed_b))
            if method == "POST":
                return 201, json.dumps(
                    {"id": "job_cli_01", "status": "queued", "model_id": "h3-vdn", "credits_cost": 27}
                ).encode("utf-8")
            return 200, json.dumps(
                {"id": "job_cli_01", "status": "succeeded", "model_id": "h3-vdn", "credits_cost": 27}
            ).encode("utf-8")

        gen_out = io.StringIO()
        gen_err = io.StringIO()
        rc = cli_main(
            [
                "generate",
                "--api-key",
                "chili_sk_validkey123456",
                "--model",
                "h3-vdn",
                "--mode",
                "i2v",
                "--prompt",
                "First to last frame transition",
                "--duration",
                "15",
                "--resolution",
                "1080p",
                "--ref-asset",
                "00000000-0000-0000-0000-000000000001",
                "--ref-asset",
                "00000000-0000-0000-0000-000000000002",
                "--ref-role",
                "first_frame",
                "--ref-role",
                "last_frame",
                "--idempotency-key",
                "cli-idem-1",
                "--wait",
                "--poll-interval",
                "0.005",
                "--json",
            ],
            stdout=gen_out,
            stderr=gen_err,
            transport=cli_transport,
        )
        self.assertEqual(rc, 0, gen_err.getvalue())
        self.assertEqual(json.loads(gen_out.getvalue())["status"], "succeeded")
        self.assertEqual(len(cli_calls), 2)
        assert cli_calls[0][2] is not None
        self.assertEqual(cli_calls[0][2]["idempotency_key"], "cli-idem-1")
        self.assertEqual(
            cli_calls[0][2]["params"],
            {
                "prompt": "First to last frame transition",
                "duration_s": 15,
                "resolution": "1080p",
                "ref_asset_ids": [
                    "00000000-0000-0000-0000-000000000001",
                    "00000000-0000-0000-0000-000000000002",
                ],
                "ref_roles": ["first_frame", "last_frame"],
            },
        )

        st_out = io.StringIO()
        st_rc = cli_main(
            ["status", "job_cli_01", "--api-key", "chili_sk_validkey123456", "--json"],
            stdout=st_out,
            stderr=io.StringIO(),
            transport=cli_transport,
        )
        self.assertEqual(st_rc, 0)
        self.assertEqual(json.loads(st_out.getvalue())["id"], "job_cli_01")

    def test_mcp_handler_and_stdio_subprocess_with_1mib_guard(self) -> None:
        init_default = handle_jsonrpc_message(
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
        )
        assert init_default is not None
        self.assertEqual(DEFAULT_PROTOCOL_VERSION, "2025-06-18")
        self.assertEqual(init_default["result"]["protocolVersion"], "2025-06-18")

        init_res = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "unittest", "version": "1.0"},
                },
            }
        )
        assert init_res is not None
        self.assertEqual(init_res["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(init_res["result"]["serverInfo"]["name"], "io.github.AI-Chili/aichili")
        self.assertEqual(init_res["result"]["serverInfo"]["version"], VERSION)

        self.assertIsNone(
            handle_jsonrpc_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

        list_res = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert list_res is not None
        tool_names = [t["name"] for t in list_res["result"]["tools"]]
        self.assertEqual(
            tool_names,
            [
                "platform_overview",
                "list_video_models",
                "estimate_video_cost",
                "get_credit_balance",
                "plan_video_generation",
                "create_video_generation",
                "get_video_generation",
            ],
        )

        est_res = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "estimate_video_cost",
                    "arguments": {
                        "model_id": "h3-sol",
                        "seconds": 15,
                        "resolution": "2k",
                        "free_signup_credits_used": True,
                        "subscribers": 10,
                        "sub_price_usd": 9.99,
                        "ppv_unlocks": 5,
                        "ppv_price_usd": 4.99,
                    },
                },
            }
        )
        assert est_res is not None
        self.assertFalse(est_res["result"]["isError"])
        self.assertEqual(est_res["result"]["structuredContent"]["totalCredits"], 38)
        self.assertEqual(
            est_res["result"]["structuredContent"]["onlyChiliProjection"]["creatorNetRevenueUsd"],
            99.88,
        )

        # Verify ref_asset_ids / ref_roles forwarding on plan_video_generation
        seen_body: dict[str, object] = {}

        def plan_transport(
            method: str,
            url: str,
            headers: dict[str, str],
            body_bytes: bytes | None,
            timeout_s: float,
        ) -> tuple[int, bytes]:
            nonlocal seen_body
            seen_body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            return 200, json.dumps({"total_credits": 15, "total_duration_s": 15, "segments": 1}).encode("utf-8")

        plan_res = handle_jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "plan_video_generation",
                    "arguments": {
                        "api_key": "chili_sk_validkey123456",
                        "mode": "i2v",
                        "model_id": "h3-sol",
                        "prompt": "Character walks forward",
                        "ref_asset_ids": ["00000000-0000-0000-0000-000000000001"],
                        "ref_roles": ["reference_image"],
                    },
                },
            },
            transport=plan_transport,
        )
        assert plan_res is not None
        self.assertFalse(plan_res["result"]["isError"])
        self.assertEqual(
            seen_body["params"],
            {
                "prompt": "Character walks forward",
                "ref_asset_ids": ["00000000-0000-0000-0000-000000000001"],
                "ref_roles": ["reference_image"],
            },
        )

        # In-memory 1 MiB guard + stream recovery test
        oversize_in = io.BytesIO(
            b"x" * (MAX_STDIO_LINE_BYTES + 200_000)
            + b"\n"
            + b'{"jsonrpc":"2.0","id":99,"method":"ping"}\n'
        )
        oversize_out = io.StringIO()
        start_mcp_stdio_server(input_stream=oversize_in, output_stream=oversize_out)
        in_mem_frames = [json.loads(ln) for ln in oversize_out.getvalue().strip().splitlines()]
        self.assertEqual(len(in_mem_frames), 2)
        self.assertEqual(in_mem_frames[0]["error"]["code"], -32700)
        self.assertIn("1 MiB", in_mem_frames[0]["error"]["message"])
        self.assertEqual(in_mem_frames[1], {"jsonrpc": "2.0", "id": 99, "result": {}})

        # Real stdio subprocess test covering initialize, tools/list, estimate_video_cost, and 1 MiB guard
        stdin_payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26"},
                }
            )
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 11, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "tools/call",
                    "params": {
                        "name": "estimate_video_cost",
                        "arguments": {"seconds": 30, "resolution": "1080p", "free_signup_credits_used": True},
                    },
                }
            )
            + "\n"
            + ("y" * (MAX_STDIO_LINE_BYTES + 1024))
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 13, "method": "ping"})
            + "\n"
        )
        proc = subprocess.run(
            [sys.executable, "-m", "aichili.mcp"],
            input=stdin_payload,
            env=self._subproc_env(),
            capture_output=True,
            text=True,
            check=True,
        )
        frames = [json.loads(line) for line in proc.stdout.strip().splitlines()]
        self.assertEqual(len(frames), 5)
        self.assertEqual(frames[0]["id"], 10)
        self.assertEqual(frames[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(frames[1]["id"], 11)
        self.assertEqual(len(frames[1]["result"]["tools"]), 7)
        self.assertEqual(frames[2]["id"], 12)
        self.assertEqual(frames[2]["result"]["structuredContent"]["totalCredits"], 54)
        self.assertEqual(frames[3]["error"]["code"], -32700)
        self.assertIn("1 MiB", frames[3]["error"]["message"])
        self.assertEqual(frames[4], {"jsonrpc": "2.0", "id": 13, "result": {}})


class TestLiveRegistrySmoke(unittest.TestCase):
    def test_live_videoapi_registry_matches_five_public_models(self) -> None:
        registry = list_models()
        self.assertIsInstance(registry, dict)
        models = registry.get("models")
        self.assertIsInstance(models, list)

        by_id: dict[str, dict[str, object]] = {}
        for m in models:
            by_id[m["model_id"]] = m
            for alias in m.get("aliases") or []:
                by_id[alias] = m

        for pub in PUBLIC_MODELS:
            live = by_id.get(pub["modelId"])
            if live is None:
                for alias in pub.get("aliases") or []:
                    if alias in by_id:
                        live = by_id[alias]
                        break
            self.assertIsNotNone(
                live, f"Expected live /v1/registry to contain '{pub['modelId']}'"
            )
            assert live is not None
            live_modes = set()
            for wf in live.get("workflows") or []:  # type: ignore[union-attr]
                if not wf.get("hidden"):
                    for md in wf.get("modes") or []:
                        live_modes.add(md)
            self.assertEqual(
                sorted(pub["modes"]),
                sorted(live_modes),
                f"PUBLIC_MODELS modes drifted from live /v1/registry for {pub['modelId']}",
            )


if __name__ == "__main__":
    unittest.main()
