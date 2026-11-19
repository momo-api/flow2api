import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler
from src.services.load_balancer import LoadBalancer


MODEL = "omni-1.2-flash-3s-portrait"
PROJECT_ID = "01234567-89ab-cdef-0123-456789abcdef"
MEDIA_ID = "11234567-89ab-cdef-0123-456789abcdef"
OP_ID = "21234567-89ab-cdef-0123-456789abcdef"
AUTH_401_ERROR = (
    "Flow 登录认证失败：Google Cookie 已失效、不完整，或来自不同账号 (rpc=YhhmEf, code=401)"
)


def _token(token_id: int, cookies: str = "old-cookies"):
    return SimpleNamespace(
        id=token_id,
        email=f"user{token_id}@example.com",
        user_paygate_tier="PAYGATE_TIER_ONE",
        image_enabled=True,
        video_enabled=True,
        credits=500,
        image_concurrency=-1,
        video_concurrency=-1,
        google_cookies=cookies,
    )


class FlowCookieReloadAfterRecaptchaTests(unittest.IsolatedAsyncioTestCase):
    def _make_client_with_rotating_db(self):
        state = {"cookies": "old-cookies"}
        db = MagicMock()
        db.get_token = AsyncMock(
            side_effect=lambda tid: _token(tid, state["cookies"])
        )
        client = FlowClient(proxy_manager=None, db=db)
        client._frontend_cookie = AsyncMock(return_value="SID=old")
        client._resolve_flow_frontend_cookie_storage = AsyncMock(
            side_effect=lambda google_cookies=None, token_id=None: (
                google_cookies or state["cookies"]
            )
        )
        client._notify_browser_captcha_request_finished = AsyncMock()
        client._get_runtime_config = MagicMock(
            return_value=SimpleNamespace(
                flow_max_retries=1,
                flow_image_request_timeout=90,
                upsample_timeout=300,
            )
        )

        async def rotate_during_captcha(*args, **kwargs):
            state["cookies"] = "fresh-cookies-after-warmup"
            return "recaptcha-tok", "personal:1"

        client._get_recaptcha_token = AsyncMock(side_effect=rotate_during_captcha)
        return client, db, state

    async def test_video_generation_reloads_fresh_cookies_after_recaptcha(self):
        client, db, _ = self._make_client_with_rotating_db()
        captured = {}

        async def fake_current_rpc(*, google_cookies=None, **kwargs):
            captured["google_cookies"] = google_cookies
            return [[[OP_ID, PROJECT_ID, MEDIA_ID]]]

        client._current_rpc = AsyncMock(side_effect=fake_current_rpc)
        result = await client._current_generate_video(
            project_id=PROJECT_ID,
            prompt="sunset beach",
            model_key="macaron_cookie_t2v_3s",
            aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT",
            mode="text",
            token_id=1,
            token_video_concurrency=-1,
            google_cookies="old-cookies",
        )

        self.assertEqual(captured["google_cookies"], "fresh-cookies-after-warmup")
        self.assertEqual(result["operations"][0]["mediaName"], MEDIA_ID)
        db.get_token.assert_awaited_once_with(1)

    async def test_image_generation_reloads_fresh_cookies_after_recaptcha(self):
        client, db, _ = self._make_client_with_rotating_db()
        captured = {}

        async def fake_current_rpc(*, google_cookies=None, **kwargs):
            captured["google_cookies"] = google_cookies
            return {"ok": True}

        client._current_rpc = AsyncMock(side_effect=fake_current_rpc)
        client._normalize_frontend_image_generation_response = MagicMock(
            return_value={"media": [{"name": MEDIA_ID}]}
        )
        result, _, _ = await client.generate_image(
            at="flow-frontend-cookie",
            project_id=PROJECT_ID,
            prompt="sunny cat",
            model_name="GEM_PIX_2",
            aspect_ratio="IMAGE_ASPECT_RATIO_PORTRAIT",
            token_id=1,
            google_cookies="old-cookies",
        )

        self.assertEqual(captured["google_cookies"], "fresh-cookies-after-warmup")
        self.assertEqual(result["media"][0]["name"], MEDIA_ID)
        db.get_token.assert_awaited_once_with(1)

    async def test_upsample_image_reloads_fresh_cookies_after_recaptcha(self):
        client, db, _ = self._make_client_with_rotating_db()
        captured = {}

        async def fake_current_rpc(*, google_cookies=None, **kwargs):
            captured["google_cookies"] = google_cookies
            return [["https://example.com/upsampled.png"]]

        client._current_rpc = AsyncMock(side_effect=fake_current_rpc)
        client._find_media_url = MagicMock(return_value="https://example.com/upsampled.png")
        url = await client.upsample_image(
            at="flow-frontend-cookie",
            project_id=PROJECT_ID,
            media_id=MEDIA_ID,
            token_id=1,
            google_cookies="old-cookies",
        )

        self.assertEqual(captured["google_cookies"], "fresh-cookies-after-warmup")
        self.assertEqual(url, "https://example.com/upsampled.png")
        db.get_token.assert_awaited_once_with(1)

    async def test_reload_cookies_falls_back_safely_when_db_missing_or_fails(self):
        client_no_db = FlowClient(proxy_manager=None, db=None)
        self.assertEqual(
            await client_no_db._reload_latest_google_cookies("old-cookies", 1),
            "old-cookies",
        )

        db_none_tok = MagicMock()
        db_none_tok.get_token = AsyncMock(return_value=None)
        client_none_tok = FlowClient(proxy_manager=None, db=db_none_tok)
        self.assertEqual(
            await client_none_tok._reload_latest_google_cookies("old-cookies", 1),
            "old-cookies",
        )
        self.assertEqual(
            await client_none_tok._reload_latest_google_cookies("old-cookies", None),
            "old-cookies",
        )

        db_err = MagicMock()
        db_err.get_token = AsyncMock(side_effect=RuntimeError("db busy"))
        client_err = FlowClient(proxy_manager=None, db=db_err)
        self.assertEqual(
            await client_err._reload_latest_google_cookies("old-cookies", 1),
            "old-cookies",
        )

    async def test_check_video_status_reloads_fresh_cookies_and_media_fallback(self):
        client, db, state = self._make_client_with_rotating_db()
        state["cookies"] = "fresh-poll-cookies"
        rpc_calls = []

        async def fake_current_rpc(*, rpc_id, google_cookies=None, **kwargs):
            rpc_calls.append((rpc_id, google_cookies))
            if rpc_id == "jwpduf":
                return [[OP_ID, PROJECT_ID, MEDIA_ID, "active"]]
            return [None, PROJECT_ID]

        client._current_rpc = AsyncMock(side_effect=fake_current_rpc)
        client._normalize_frontend_video_status = MagicMock(
            return_value={
                "operations": [
                    {
                        "name": OP_ID,
                        "mediaName": MEDIA_ID,
                        "status": "MEDIA_GENERATION_STATUS_ACTIVE",
                        "operation": {"name": OP_ID, "metadata": {"video": {}}},
                    }
                ]
            }
        )
        client._find_media_url = MagicMock(return_value="https://example.com/video/1.mp4")

        result = await client.check_video_status(
            "flow-frontend-cookie",
            [{"name": OP_ID, "projectId": PROJECT_ID}],
            token_id=1,
            google_cookies="stale-pre-captcha-cookies",
        )

        self.assertEqual(rpc_calls[0], ("jwpduf", "fresh-poll-cookies"))
        self.assertEqual(rpc_calls[1], ("as29s", "fresh-poll-cookies"))
        self.assertEqual(
            result["operations"][0]["operation"]["metadata"]["video"]["fifeUrl"],
            "https://example.com/video/1.mp4",
        )
        self.assertGreaterEqual(db.get_token.await_count, 2)

    async def test_upload_image_reloads_fresh_cookies_after_recaptcha(self):
        client, db, _ = self._make_client_with_rotating_db()
        captured = {}

        async def fake_current_rpc(*, rpc_id, google_cookies=None, **kwargs):
            captured["rpc_id"] = rpc_id
            captured["google_cookies"] = google_cookies
            return [[f"https://example.com/image/{MEDIA_ID}"]]

        client._current_rpc = AsyncMock(side_effect=fake_current_rpc)
        media_id = await client.upload_image(
            "flow-frontend-cookie",
            b"\x89PNG\r\n\x1a\nfake",
            project_id=PROJECT_ID,
            token_id=1,
            google_cookies="old-cookies",
        )

        self.assertEqual(captured["rpc_id"], "maseQ")
        self.assertEqual(captured["google_cookies"], "fresh-cookies-after-warmup")
        self.assertEqual(media_id, MEDIA_ID)
        db.get_token.assert_awaited_once_with(1)

    async def test_wait_for_video_media_ready_reloads_fresh_cookies(self):
        client, db, state = self._make_client_with_rotating_db()
        state["cookies"] = "fresh-ready-cookies"
        captured = {}

        async def fake_current_rpc(*, rpc_id, google_cookies=None, **kwargs):
            captured["rpc_id"] = rpc_id
            captured["google_cookies"] = google_cookies
            return [MEDIA_ID, PROJECT_ID, "https://example.com/video/ready.mp4"]

        client._current_rpc = AsyncMock(side_effect=fake_current_rpc)
        client._find_flow_content_url = MagicMock(return_value="https://example.com/video/ready.mp4")
        await client.wait_for_video_media_ready(
            MEDIA_ID,
            PROJECT_ID,
            google_cookies="stale-cookies",
            token_id=1,
            timeout=5,
            poll_interval=0.1,
        )

        self.assertEqual(captured["rpc_id"], "as29s")
        self.assertEqual(captured["google_cookies"], "fresh-ready-cookies")
        db.get_token.assert_awaited_once_with(1)


class GenerationHandlerAuth401RetryTests(unittest.IsolatedAsyncioTestCase):
    def _build_handler(self, *, fail_count: int, refresh_ok: bool = True):
        token = _token(1, "old-cookies")
        token_manager = MagicMock()
        token_manager.get_active_tokens = AsyncMock(return_value=[token])
        token_manager.ensure_valid_token = AsyncMock(side_effect=lambda t: t)
        token_manager.ensure_project_exists = AsyncMock(return_value=PROJECT_ID)
        token_manager.needs_at_refresh = MagicMock(return_value=False)
        token_manager.record_error = AsyncMock()
        token_manager.record_usage = AsyncMock()
        token_manager.record_success = AsyncMock()

        async def fake_refresh_cookies(tid):
            if refresh_ok:
                token.google_cookies = "refreshed-cookies"
            return refresh_ok

        token_manager.refresh_cookies = AsyncMock(side_effect=fake_refresh_cookies)

        load_balancer = LoadBalancer(token_manager)
        load_balancer._check_extension_route = AsyncMock(return_value=(True, ""))

        handler = GenerationHandler.__new__(GenerationHandler)
        handler.token_manager = token_manager
        handler.load_balancer = load_balancer
        handler.flow_client = MagicMock()
        handler.flow_client.prefill_remote_browser_pool = AsyncMock()
        handler.flow_client.clear_request_fingerprint = MagicMock()
        handler._log_request = AsyncMock(return_value=1)
        handler._update_request_log_progress = AsyncMock()

        calls = {"count": 0, "seen_cookies": []}

        async def fake_video(selected_token, *args, generation_result=None, **kwargs):
            calls["count"] += 1
            calls["seen_cookies"].append(selected_token.google_cookies)
            if calls["count"] <= fail_count:
                raise RuntimeError(AUTH_401_ERROR)
            handler._mark_generation_succeeded(generation_result)
            yield handler._create_completion_response(
                "https://media.momoapi.us/video-temp/out/demo.mp4",
                media_type="video",
            )

        handler._handle_video_generation = fake_video
        return handler, token_manager, calls

    def test_is_flow_auth_401_error_helper(self):
        self.assertTrue(
            GenerationHandler._is_flow_auth_401_error(RuntimeError(AUTH_401_ERROR))
        )
        self.assertFalse(
            GenerationHandler._is_flow_auth_401_error(
                RuntimeError("Flow frontend RPC rejected: rpc=YhhmEf, code=[3]")
            )
        )
        self.assertFalse(
            GenerationHandler._is_flow_auth_401_error(
                RuntimeError("Flow upstream HTTP 403")
            )
        )

    async def test_refreshes_cookies_once_and_retries_same_token_on_401(self):
        handler, token_manager, calls = self._build_handler(
            fail_count=1, refresh_ok=True
        )
        with patch("src.services.generation_handler.record_generation_result"):
            chunks = [chunk async for chunk in handler.handle_generation(MODEL, "cat")]

        self.assertEqual(calls["count"], 2)
        self.assertEqual(calls["seen_cookies"], ["old-cookies", "refreshed-cookies"])
        token_manager.refresh_cookies.assert_awaited_once_with(1)
        token_manager.record_error.assert_not_awaited()
        token_manager.record_success.assert_awaited_once_with(1)
        self.assertIn("https://media.momoapi.us/video-temp/out/demo.mp4", chunks[-1])

    async def test_stops_after_single_refresh_when_401_persists(self):
        handler, token_manager, calls = self._build_handler(
            fail_count=5, refresh_ok=True
        )
        with patch("src.services.generation_handler.record_generation_result"):
            chunks = [chunk async for chunk in handler.handle_generation(MODEL, "cat")]

        self.assertEqual(calls["count"], 2)
        token_manager.refresh_cookies.assert_awaited_once_with(1)
        token_manager.record_error.assert_awaited_once_with(1)
        err = json.loads(chunks[-1])["error"]
        self.assertIn("code=401", err["message"])

    async def test_poll_video_result_refreshes_cookies_once_on_401_then_succeeds(self):
        token = _token(1, "stale-poll-cookies")
        token.at = "flow-frontend-cookie"
        token.video_concurrency = -1
        token_manager = MagicMock()
        refreshed_tok = _token(1, "fresh-poll-cookies")

        async def fake_refresh_cookies(tid):
            token.google_cookies = "fresh-poll-cookies"
            return True

        token_manager.refresh_cookies = AsyncMock(side_effect=fake_refresh_cookies)
        token_manager.get_token = AsyncMock(return_value=refreshed_tok)

        handler = GenerationHandler.__new__(GenerationHandler)
        handler.token_manager = token_manager
        handler.load_balancer = MagicMock()
        handler.db = MagicMock()
        handler.db.update_task = AsyncMock()
        handler.flow_client = MagicMock()
        handler.flow_client.download_video_bytes = AsyncMock()

        poll_401 = (
            "Flow 登录认证失败：Google Cookie 已失效、不完整，或来自不同账号 (rpc=jwpduf, code=401)"
        )
        seen_poll_cookies = []
        poll_count = {"n": 0}

        async def fake_check_video_status(at, operations, token_id=None, google_cookies=None):
            poll_count["n"] += 1
            seen_poll_cookies.append(google_cookies)
            if poll_count["n"] == 1:
                raise RuntimeError(poll_401)
            return {
                "operations": [
                    {
                        "name": OP_ID,
                        "mediaName": MEDIA_ID,
                        "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL",
                        "operation": {
                            "name": OP_ID,
                            "metadata": {
                                "video": {
                                    "aspectRatio": "VIDEO_ASPECT_RATIO_PORTRAIT",
                                    "mediaName": MEDIA_ID,
                                }
                            },
                        },
                    }
                ]
            }

        handler.flow_client.check_video_status = AsyncMock(side_effect=fake_check_video_status)
        handler._resolve_video_asset = AsyncMock(
            return_value={
                "video_url": "https://example.com/upstream/video.mp4",
                "video_media_id": MEDIA_ID,
                "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
                "media_name": MEDIA_ID,
                "metadata": {},
                "video_info": {},
            }
        )
        handler._fail_video_task = AsyncMock()
        generation_result = handler._create_generation_result()
        operations = [{"name": OP_ID, "projectId": PROJECT_ID, "operation": {"name": OP_ID}}]

        with (
            patch("src.services.generation_handler.r2_storage.is_r2_video_storage_configured", return_value=False),
            patch(
                "src.services.generation_handler.config",
                SimpleNamespace(poll_interval=0.01, video_timeout=5, cache_enabled=False),
            ),
        ):
            chunks = [
                chunk
                async for chunk in handler._poll_video_result(
                    token,
                    PROJECT_ID,
                    operations,
                    stream=False,
                    generation_result=generation_result,
                )
            ]

        self.assertEqual(poll_count["n"], 2)
        self.assertEqual(seen_poll_cookies, ["stale-poll-cookies", "fresh-poll-cookies"])
        token_manager.refresh_cookies.assert_awaited_once_with(1)
        handler._fail_video_task.assert_not_awaited()
        self.assertTrue(generation_result["success"])
        self.assertIn("https://example.com/upstream/video.mp4", chunks[-1])

    async def test_poll_video_result_stops_after_max_errors_if_401_persists(self):
        token = _token(1, "stale-poll-cookies")
        token.at = "flow-frontend-cookie"
        token_manager = MagicMock()
        token_manager.refresh_cookies = AsyncMock(return_value=True)
        token_manager.get_token = AsyncMock(return_value=_token(1, "fresh-poll-cookies"))

        handler = GenerationHandler.__new__(GenerationHandler)
        handler.token_manager = token_manager
        handler.flow_client = MagicMock()
        poll_401 = (
            "Flow 登录认证失败：Google Cookie 已失效、不完整，或来自不同账号 (rpc=jwpduf, code=401)"
        )
        handler.flow_client.check_video_status = AsyncMock(side_effect=RuntimeError(poll_401))
        handler._fail_video_task = AsyncMock()
        generation_result = handler._create_generation_result()
        operations = [{"name": OP_ID, "projectId": PROJECT_ID, "operation": {"name": OP_ID}}]

        with patch(
            "src.services.generation_handler.config",
            SimpleNamespace(poll_interval=0.01, video_timeout=5, cache_enabled=False),
        ):
            chunks = [
                chunk
                async for chunk in handler._poll_video_result(
                    token,
                    PROJECT_ID,
                    operations,
                    stream=False,
                    generation_result=generation_result,
                )
            ]

        # Attempt 1 triggers single refresh_cookies (without incrementing poll error),
        # then 3 consecutive poll failures terminate cleanly.
        self.assertEqual(handler.flow_client.check_video_status.await_count, 4)
        token_manager.refresh_cookies.assert_awaited_once_with(1)
        handler._fail_video_task.assert_awaited_once()
        self.assertFalse(generation_result["success"])
        self.assertIn("视频状态查询失败", chunks[-1])


if __name__ == "__main__":
    unittest.main()
