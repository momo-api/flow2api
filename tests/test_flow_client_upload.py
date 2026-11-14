import unittest
from unittest.mock import AsyncMock, patch

from src.services.flow_client import FlowClient


JPEG_BYTES = b"\xff\xd8\xff" + b"0" * 16
PROJECT_ID = "01234567-89ab-cdef-0123-456789abcdef"
MEDIA_ID = "11234567-89ab-cdef-0123-456789abcdef"


class FlowClientUploadImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_upload_uses_resumable_flow_protocol(self):
        client = FlowClient(proxy_manager=None)
        client._resolve_flow_frontend_cookie_storage = AsyncMock(return_value="SID=session-cookie")
        client._get_effective_request_user_agent = lambda: "Mozilla/5.0 Chrome/157.0.0.0"
        client._resolve_runtime_impersonate = lambda: "chrome"
        client._get_control_plane_timeout = lambda: 30
        client.get_request_fingerprint = lambda: {}
        client._update_flow_cookie_jar = lambda *_: None
        client._remember_flow_cookies = lambda *_: None

        class Response:
            def __init__(self, status, headers=None, payload=None):
                self.status_code = status
                self.headers = headers or {}
                self._payload = payload
            def json(self):
                return self._payload

        class Session:
            def __init__(self):
                self.curl_options = {}
                self.calls = []
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False
            async def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if len(self.calls) == 1:
                    return Response(200, {"x-goog-upload-url": "https://flow.google.com/upload/v1/flow/upload/video/" + PROJECT_ID + "?upload_id=test"})
                return Response(200, payload=[MEDIA_ID, PROJECT_ID])

        session = Session()
        with patch("src.services.flow_current.AsyncSession", return_value=session):
            media_id = await client.upload_video(
                b"video-bytes", PROJECT_ID, token_id=None, google_cookies="SID=session-cookie"
            )
        self.assertEqual(media_id, MEDIA_ID)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0][1]["headers"]["X-Goog-Upload-Command"], "start")
        self.assertEqual(session.calls[1][1]["headers"]["X-Goog-Upload-Command"], "upload, finalize")
        self.assertNotIn("X-Goog-Upload-Header-Content-Type", session.calls[0][1]["headers"])
        self.assertNotIn("X-Goog-Upload-Header-Content-Length", session.calls[1][1]["headers"])

    async def test_video_readiness_waits_for_matching_media_video_url(self):
        client = FlowClient(proxy_manager=None)
        client._current_rpc = AsyncMock(side_effect=[
            [MEDIA_ID, PROJECT_ID, "processing"],
            [PROJECT_ID, MEDIA_ID, "https://flow-content.google/video/wrong-media"],
            [MEDIA_ID, PROJECT_ID, "https://flow-content.google/video/ready"],
        ])
        with patch("src.services.flow_current.asyncio.sleep", new=AsyncMock()):
            await client.wait_for_video_media_ready(
                MEDIA_ID, PROJECT_ID, google_cookies="SID=session-cookie", token_id=1,
            )
        self.assertEqual(client._current_rpc.await_count, 3)
        self.assertEqual(client._current_rpc.await_args.kwargs["rpc_id"], "as29s")
        self.assertEqual(client._current_rpc.await_args.kwargs["argument"], [MEDIA_ID])

    async def test_video_readiness_stops_on_processing_error(self):
        client = FlowClient(proxy_manager=None)
        client._current_rpc = AsyncMock(return_value=["PUBLIC_ERROR_UNSAFE_GENERATION"])
        with self.assertRaisesRegex(RuntimeError, "processing failed"):
            await client.wait_for_video_media_ready(MEDIA_ID, PROJECT_ID)

    async def test_video_readiness_deadline_bounds_slow_rpc(self):
        import asyncio
        client = FlowClient(proxy_manager=None)
        async def slow_rpc(**kwargs):
            await asyncio.sleep(1)
        client._current_rpc = AsyncMock(side_effect=slow_rpc)
        with self.assertRaises(TimeoutError):
            await client.wait_for_video_media_ready(MEDIA_ID, PROJECT_ID, timeout=0.01)

    async def test_video_readiness_rejects_invalid_ids_without_rpc(self):
        client = FlowClient(proxy_manager=None)
        client._current_rpc = AsyncMock()
        with self.assertRaises(ValueError):
            await client.wait_for_video_media_ready("not-a-uuid", PROJECT_ID)
        client._current_rpc.assert_not_awaited()

    async def test_upload_uses_current_frontend_rpc(self):
        client = FlowClient(proxy_manager=None)
        client._get_recaptcha_token = AsyncMock(return_value=("captcha-token", None))
        client._call_flow_frontend_rpc = AsyncMock(
            return_value=[
                [
                    MEDIA_ID,
                    PROJECT_ID,
                    f"https://flow-content.google/image/{MEDIA_ID}?Signature=1",
                ]
            ]
        )
        client._make_request = AsyncMock()

        media_id = await client.upload_image(
            at="frontend-cookie",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id=PROJECT_ID,
            google_cookies="SID=session-cookie",
        )

        self.assertEqual(media_id, MEDIA_ID)
        client._make_request.assert_not_awaited()
        request = client._call_flow_frontend_rpc.await_args.kwargs
        self.assertEqual(request["rpc_id"], "maseQ")
        self.assertEqual(request["argument"][0][5], PROJECT_ID)
        self.assertEqual(request["argument"][0][10], ["captcha-token", 1])
        self.assertEqual(request["argument"][2], "image/jpeg")

    async def test_upload_argument_matches_current_frontend_envelope(self):
        client = FlowClient(proxy_manager=None)
        client._get_recaptcha_token = AsyncMock(return_value=("captcha-token", None))
        client._call_flow_frontend_rpc = AsyncMock(
            return_value=[[MEDIA_ID, PROJECT_ID, f"https://flow-content.google/image/{MEDIA_ID}?Signature=1"]]
        )
        client._make_request = AsyncMock()

        await client.upload_image(
            at="frontend-cookie",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id=PROJECT_ID,
            google_cookies="SID=session-cookie",
        )

        argument = client._call_flow_frontend_rpc.await_args.kwargs["argument"]
        self.assertEqual(len(argument), 12)
        # slot 3 is an integer enum; sending a bool makes the RPC reject with code=[3]
        self.assertIs(type(argument[3]), int)
        self.assertIsNone(argument[7])
        self.assertIsNone(argument[9])
        for slot in (10, 11):
            self.assertRegex(argument[slot], r"^[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}$")

    async def test_upload_requires_project_id(self):
        client = FlowClient(proxy_manager=None)

        with self.assertRaisesRegex(ValueError, "requires project_id"):
            await client.upload_image(
                at="frontend-cookie",
                image_bytes=JPEG_BYTES,
                project_id=None,
                google_cookies="SID=session-cookie",
            )

    async def test_video_edit_uses_current_jips6_payload(self):
        client = FlowClient(proxy_manager=None)
        client._current_generate_video = AsyncMock(return_value={"ok": True})
        result = await client.generate_video_edit(
            at="frontend-cookie",
            project_id=PROJECT_ID,
            prompt="make it dance",
            source_media_id=MEDIA_ID,
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            source_start_frame=0,
            source_end_frame=184,
            google_cookies="SID=session-cookie",
        )
        self.assertEqual(result, {"ok": True})
        kwargs = client._current_generate_video.await_args.kwargs
        self.assertEqual(kwargs["mode"], "edit")
        self.assertEqual(kwargs["model_key"], "macaron_cookie_edit_360p")
        self.assertEqual(kwargs["source_start_frame"], 0)
        self.assertEqual(kwargs["source_end_frame"], 184)


if __name__ == "__main__":
    unittest.main()
