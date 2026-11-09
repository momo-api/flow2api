"""Regression tests for Account Pool hardening, security fixes, and NewAPI endpoints."""

import asyncio
import base64
from io import BytesIO
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import httpx
from fastapi import FastAPI
from PIL import Image

from src.api import routes
from src.core import config as config_mod
from src.core.account_tiers import (
    PAYGATE_TIER_NOT_PAID,
    PAYGATE_TIER_ONE,
    PAYGATE_TIER_TWO,
    supports_model_for_tier,
)
from src.core.auth import verify_api_key_flexible
from src.core.config import config, normalize_captcha_method
from src.core.database import Database
from src.core.models import Token
from src.services.concurrency_manager import ConcurrencyManager
from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler
from src.services.load_balancer import LoadBalancer


def _make_png_bytes(width: int = 64, height: int = 64) -> bytes:
    img = Image.new("RGB", (width, height), color="blue")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class PoolSecurityAndTierTests(unittest.IsolatedAsyncioTestCase):
    def test_team_captcha_backdoor_is_removed(self):
        self.assertFalse(hasattr(config_mod, "FLOW2API_TEAM_CAPTCHA_BASE_URL"))
        self.assertFalse(hasattr(config_mod, "FLOW2API_TEAM_CAPTCHA_CLIENT_KEY"))
        self.assertFalse(hasattr(FlowClient, "_build_team_captcha_cookie_map"))
        self.assertEqual(normalize_captcha_method("flow2api_team"), "yescaptcha")

    async def test_get_credits_detects_ultra_20000_credits_as_tier_two(self):
        client = FlowClient(proxy_manager=None)
        client._current_rpc = AsyncMock(return_value=[20000])
        res = await client.get_credits("at", google_cookies="SID=1")
        self.assertEqual(res["credits"], 20000)
        self.assertEqual(res["userPaygateTier"], PAYGATE_TIER_TWO)

        client._current_rpc = AsyncMock(return_value=[500])
        res_pro = await client.get_credits("at", google_cookies="SID=1")
        self.assertEqual(res_pro["userPaygateTier"], PAYGATE_TIER_ONE)

        client._current_rpc = AsyncMock(return_value=[0])
        res_free = await client.get_credits("at", google_cookies="SID=1")
        self.assertEqual(res_free["userPaygateTier"], PAYGATE_TIER_NOT_PAID)

    def test_paid_tiers_can_use_4k_and_ultra_models(self):
        for model_name in (
            "gemini-3.1-flash-image-landscape-4k",
            "veo_3_1_t2v_4s_4k",
            "veo_3_1_t2v_fast_ultra",
        ):
            self.assertTrue(supports_model_for_tier(model_name, PAYGATE_TIER_TWO))
            self.assertTrue(supports_model_for_tier(model_name, PAYGATE_TIER_ONE))
            self.assertFalse(supports_model_for_tier(model_name, PAYGATE_TIER_NOT_PAID))

    async def test_retrieve_image_data_blocks_path_traversal_and_ssrf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            cache_dir = tmp_root / "tmp"
            cache_dir.mkdir()
            secret_file = tmp_root / "secret.txt"
            secret_file.write_text("TOP_SECRET", encoding="utf-8")
            valid_img = cache_dir / "valid.png"
            png_bytes = _make_png_bytes()
            valid_img.write_bytes(png_bytes)

            fake_handler = SimpleNamespace(
                file_cache=SimpleNamespace(cache_dir=cache_dir)
            )
            with patch.object(routes, "generation_handler", fake_handler):
                self.assertEqual(
                    await routes.retrieve_image_data("http://localhost:8000/tmp/valid.png"),
                    png_bytes,
                )
                self.assertIsNone(
                    await routes.retrieve_image_data(
                        "http://localhost:8000/tmp/../secret.txt"
                    )
                )
                self.assertIsNone(
                    await routes.retrieve_image_data("/tmp/../../data/flow.db")
                )
                self.assertIsNone(
                    await routes.retrieve_image_data("http://127.0.0.1:8000/admin")
                )
                self.assertIsNone(
                    await routes.retrieve_image_data(
                        "http://169.254.169.254/latest/meta-data/"
                    )
                )


class ConcurrencyAndFailoverFixesTests(unittest.IsolatedAsyncioTestCase):
    async def test_failover_continues_when_first_token_at_refresh_fails(self):
        t1 = SimpleNamespace(
            id=1,
            email="expired@example.com",
            user_paygate_tier=PAYGATE_TIER_TWO,
            image_enabled=True,
            video_enabled=True,
            credits=20000,
            image_concurrency=2,
            video_concurrency=2,
        )
        t2 = SimpleNamespace(
            id=2,
            email="healthy@example.com",
            user_paygate_tier=PAYGATE_TIER_TWO,
            image_enabled=True,
            video_enabled=True,
            credits=20000,
            image_concurrency=2,
            video_concurrency=2,
        )
        token_manager = MagicMock()
        token_manager.get_active_tokens = AsyncMock(return_value=[t1, t2])
        # LB select_token calls ensure_valid_token once, then handle_generation calls it once
        token_manager.ensure_valid_token = AsyncMock(
            side_effect=[t1, None, t2, t2]
        )
        token_manager.ensure_project_exists = AsyncMock(return_value="proj-2")
        token_manager.needs_at_refresh = MagicMock(return_value=False)
        token_manager.record_error = AsyncMock()
        token_manager.record_usage = AsyncMock()
        token_manager.record_success = AsyncMock()

        lb = LoadBalancer(token_manager)
        lb._check_extension_route = AsyncMock(return_value=(True, ""))

        handler = GenerationHandler.__new__(GenerationHandler)
        handler.token_manager = token_manager
        handler.load_balancer = lb
        handler.flow_client = MagicMock()
        handler.flow_client.prefill_remote_browser_pool = AsyncMock()
        handler._log_request = AsyncMock(return_value=1)
        handler._update_request_log_progress = AsyncMock()

        used_tokens = []

        async def fake_image(token, *args, generation_result=None, **kwargs):
            used_tokens.append(token.id)
            handler._mark_generation_succeeded(generation_result)
            yield handler._create_completion_response(
                "https://example.com/out.png", media_type="image"
            )

        handler._handle_image_generation = fake_image
        with (
            patch("src.services.generation_handler.record_generation_result"),
            patch(
                "src.services.load_balancer.random.random",
                side_effect=[0.1, 0.9, 0.1, 0.9],
            ),
        ):
            chunks = [
                c
                async for c in handler.handle_generation(
                    "gemini-3.1-flash-image-landscape", "cat"
                )
            ]

        self.assertEqual(used_tokens, [2])
        self.assertIn("https://example.com/out.png", chunks[-1])

    async def test_image_generation_enforces_concurrency_and_caches_upsampled_url(self):
        token = SimpleNamespace(
            id=7,
            at="at",
            st="st",
            email="ult@example.com",
            user_paygate_tier=PAYGATE_TIER_TWO,
            image_enabled=True,
            video_enabled=True,
            credits=20000,
            image_concurrency=1,
            video_concurrency=1,
            google_cookies="SID=1",
        )
        concurrency_mgr = ConcurrencyManager()
        await concurrency_mgr.initialize([token])

        flow_client = MagicMock()
        flow_client.generate_image = AsyncMock(
            return_value=(
                {
                    "media": [
                        {
                            "name": "media-1",
                            "image": {
                                "generatedImage": {
                                    "fifeUrl": "https://flow-content.google/image/1k"
                                }
                            },
                        }
                    ]
                },
                "sess-1",
                {},
            )
        )
        flow_client.upsample_image = AsyncMock(
            return_value="https://flow-content.google/image/4k"
        )

        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = flow_client
        handler.concurrency_manager = concurrency_mgr
        handler.file_cache = MagicMock()
        handler.file_cache.download_and_cache = AsyncMock(
            return_value="cached_4k.jpg"
        )
        handler._update_request_log_progress = AsyncMock()
        handler._get_base_url = MagicMock(return_value="http://localhost:8000")

        gen_result = handler._create_generation_result()
        resp_state = handler._create_response_state()
        model_cfg = {
            "type": "image",
            "model_name": "BELUGA",
            "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
            "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K",
        }

        with patch.object(
            type(config), "cache_enabled", new_callable=PropertyMock, return_value=True
        ):
            chunks = [
                c
                async for c in handler._handle_image_generation(
                    token,
                    "proj-1",
                    model_cfg,
                    "a lighthouse",
                    None,
                    False,
                    generation_result=gen_result,
                    response_state=resp_state,
                )
            ]

        self.assertTrue(gen_result["success"])
        handler.file_cache.download_and_cache.assert_awaited_once_with(
            "https://flow-content.google/image/4k", "image"
        )
        self.assertEqual(
            resp_state["url"], "http://localhost:8000/tmp/cached_4k.jpg"
        )
        self.assertEqual(await concurrency_mgr.get_image_inflight(7), 0)
        payload = json.loads(chunks[-1])
        self.assertIn("http://localhost:8000/tmp/cached_4k.jpg", payload["url"])


class NewApiRoutesCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmpdir.name) / "flow.db"))
        await self.db.init_db()

        self.token = Token(
            id=1,
            st="st-1",
            at="flow-frontend-cookie",
            email="ultra@example.com",
            credits=20000,
            user_paygate_tier=PAYGATE_TIER_TWO,
            image_enabled=True,
            video_enabled=True,
            image_concurrency=2,
            video_concurrency=2,
        )
        await self.db.add_token(self.token)

        self.handler = MagicMock()
        self.handler.db = self.db
        self.handler.token_manager = MagicMock()
        self.handler.token_manager.get_all_tokens = AsyncMock(
            return_value=[self.token]
        )
        self.handler.load_balancer = LoadBalancer(self.handler.token_manager)
        self.handler.concurrency_manager = ConcurrencyManager()
        await self.handler.concurrency_manager.initialize([self.token])

        async def fake_handle_generation(
            model, prompt, images=None, stream=False, **kwargs
        ):
            if "veo" in model or "omni" in model:
                yield json.dumps(
                    {
                        "id": "chatcmpl-1",
                        "object": "chat.completion",
                        "created": 123,
                        "model": "flow2api",
                        "url": "https://example.com/generated.mp4",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "<video src='https://example.com/generated.mp4' controls></video>",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                    }
                )
            else:
                yield json.dumps(
                    {
                        "id": "chatcmpl-1",
                        "object": "chat.completion",
                        "created": 123,
                        "model": "flow2api",
                        "url": "https://example.com/generated.png",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "![Generated Image](https://example.com/generated.png)",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                    }
                )

        self.handler.handle_generation = fake_handle_generation
        routes.set_generation_handler(self.handler)

        self.app = FastAPI()
        self.app.include_router(routes.router)
        self.app.dependency_overrides[verify_api_key_flexible] = lambda: "test-key"

    async def asyncTearDown(self):
        self.tmpdir.cleanup()

    async def test_openai_images_generations_and_edits_endpoints(self):
        png_bytes = _make_png_bytes(100, 100)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        ) as client:
            resp = await client.post(
                "/v1/images/generations",
                json={
                    "model": "gemini-3.1-flash-image",
                    "prompt": "A futuristic city",
                    "size": "1792x1024",
                    "quality": "hd",
                    "response_format": "url",
                },
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(
                body["data"][0]["url"], "https://example.com/generated.png"
            )
            self.assertIn("usage", body)

            with patch.object(
                routes, "retrieve_image_data", AsyncMock(return_value=png_bytes)
            ):
                edit_resp = await client.post(
                    "/v1/images/edits",
                    data={
                        "model": "gemini-3.1-flash-image",
                        "prompt": "Make it winter",
                        "response_format": "b64_json",
                    },
                    files={"image": ("input.png", png_bytes, "image/png")},
                )
            self.assertEqual(edit_resp.status_code, 200)
            edit_body = edit_resp.json()
            self.assertEqual(
                base64.b64decode(edit_body["data"][0]["b64_json"]), png_bytes
            )

    async def test_async_video_generation_and_polling_endpoints(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        ) as client:
            submit_resp = await client.post(
                "/v1/video/generations",
                json={
                    "model": "omni-1.2-flash",
                    "prompt": "A cat surfing",
                },
            )
            self.assertEqual(submit_resp.status_code, 200)
            task_data = submit_resp.json()
            task_id = task_data["task_id"]
            self.assertTrue(task_id.startswith("video-"))

            await asyncio.sleep(0.05)
            poll_resp = await client.get(f"/v1/video/generations/{task_id}")
            self.assertEqual(poll_resp.status_code, 200)
            polled = poll_resp.json()
            self.assertEqual(polled["status"], "completed")
            self.assertEqual(polled["progress"], 100)
            self.assertEqual(
                polled["video_url"], "https://example.com/generated.mp4"
            )

    async def test_account_pool_status_endpoint(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        ) as client:
            resp = await client.get("/v1/pool/status")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["total_accounts"], 1)
            self.assertEqual(data["active_accounts"], 1)
            self.assertEqual(data["total_credits"], 20000)
            self.assertEqual(data["tiers"]["Ult"], 1)


if __name__ == "__main__":
    unittest.main()
