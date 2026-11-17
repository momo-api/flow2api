import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

from curl_cffi import CurlInfo
from httpx import ASGITransport, AsyncClient

from main import app
from src.api import routes
from src.core.config import config
from src.core.database import Database
from src.services import r2_storage
from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler, MODEL_CONFIG

PROJECT_ID = "718a0097-901a-4fe5-99aa-71991c57b192"
VIDEO_ID = "b1d51f56-141b-45c7-866f-6af4119880f7"
IMG_ID_1 = "9619ed65-a11e-471a-aa71-5a1eab08506c"
IMG_ID_2 = "de4551be-99be-47a8-b33e-7bcbcbb3af3e"
IMG_ID_3 = "82ab312f-6ae2-479f-ad8b-d6eb580fe1b8"
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
WEBM_BYTES = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01"
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"


def _rpc_ok(rpc_id, payload):
    frame = json.dumps([["wrb.fr", rpc_id, json.dumps(payload), None, None, None, "generic"]])
    return f")]}}'\n\n{len(frame)}\n{frame}\n"


class _DummyTokenManager:
    def __init__(self):
        self.record_usage = AsyncMock()
        self.record_error = AsyncMock()
        self.record_success = AsyncMock()


class _DummyConcurrency:
    async def wait_acquire_video(self, token_id, timeout_seconds=None):
        return True, 0

    async def acquire_video(self, token_id):
        return True

    async def release_video(self, token_id):
        return None


class OmniR2AndMaterialTests(unittest.IsolatedAsyncioTestCase):
    def test_jips6_payload_places_up_to_three_references_in_slot_8(self):
        client = FlowClient(proxy_manager=None)
        rpc_id, argument = client._build_frontend_video_generation_argument(
            project_id=PROJECT_ID,
            prompt="swap subject and keep motion",
            model_key="macaron_cookie_edit_360p",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            recaptcha_token="captcha-token",
            session_id="4A038373-A788-44AF-9FA5-5C203A5FE307",
            mode="edit",
            start_media_id=VIDEO_ID,
            reference_media_ids=[IMG_ID_1, IMG_ID_2, IMG_ID_3],
            source_start_frame=0,
            source_end_frame=245,
            resolution="360p",
        )
        self.assertEqual(rpc_id, "jIps6")
        req = argument[0][0]
        self.assertEqual(req[0], [None, VIDEO_ID, 0, 245, None, [], [10, 208333333]])
        self.assertEqual(req[2], "macaron_cookie_edit_360p")
        self.assertEqual(
            req[8],
            [[None, IMG_ID_1], [None, IMG_ID_2], [None, IMG_ID_3]],
        )
        self.assertEqual(req[12], [4])

        with self.assertRaisesRegex(ValueError, "at most 3"):
            client._build_frontend_video_generation_argument(
                project_id=PROJECT_ID,
                prompt="too many refs",
                model_key="macaron_cookie_edit_360p",
                aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
                recaptcha_token="captcha-token",
                session_id="4A038373-A788-44AF-9FA5-5C203A5FE307",
                mode="edit",
                start_media_id=VIDEO_ID,
                reference_media_ids=[IMG_ID_1, IMG_ID_2, IMG_ID_3, PROJECT_ID],
                source_start_frame=0,
                source_end_frame=245,
                resolution="360p",
            )

    async def test_bootstrap_cache_reuses_metadata_and_recovers_on_rpc_failure(self):
        client = FlowClient(proxy_manager=None)
        bootstrap_html = (
            "boq_labs-ai-sandbox-frontend_20261116.01_p0 "
            '{"FdrFJe":"987654321", "SNlM0e":"AT_ONE"}'
        )
        client._load_flow_frontend_bootstrap = AsyncMock(return_value=bootstrap_html)
        client._make_text_request = AsyncMock(
            side_effect=[
                _rpc_ok("maseQ", {"call": 1}),
                _rpc_ok("as29s", {"call": 2}),
                RuntimeError("stale bl"),
                _rpc_ok("jIps6", {"call": 3}),
            ]
        )

        await client._call_flow_frontend_rpc(
            rpc_id="maseQ",
            argument=["img1"],
            cookie_header="OSID=cookie-a",
            timeout=15,
            project_id=PROJECT_ID,
        )
        await client._call_flow_frontend_rpc(
            rpc_id="as29s",
            argument=[VIDEO_ID],
            cookie_header="OSID=cookie-a",
            timeout=15,
            project_id=PROJECT_ID,
        )
        # Same account & project: only 1 bootstrap HTML fetch across both RPCs
        self.assertEqual(client._load_flow_frontend_bootstrap.await_count, 1)

        # Call 3 hits RuntimeError("stale bl") on cached entry -> auto invalidates cache and retries fresh bootstrap once
        res3 = await client._call_flow_frontend_rpc(
            rpc_id="jIps6",
            argument=["edit"],
            cookie_header="OSID=cookie-a",
            timeout=15,
            project_id=PROJECT_ID,
        )
        self.assertEqual(res3, {"call": 3})
        self.assertEqual(client._load_flow_frontend_bootstrap.await_count, 2)

    async def test_chunked_video_upload_and_transient_retry(self):
        client = FlowClient(proxy_manager=None)
        large_video = b"\x00\x00\x00\x18ftypmp42" + b"A" * (9 * 1024 * 1024)
        posted_chunks = []

        class _FakeSession:
            def __init__(self, **kwargs):
                self.curl_options = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, headers=None, data=None, **kwargs):
                cmd = headers.get("X-Goog-Upload-Command")
                posted_chunks.append((url, cmd, headers.get("X-Goog-Upload-Offset"), len(data or b"")))
                if cmd == "start":
                    return SimpleNamespace(
                        status_code=200,
                        url=url,
                        headers={"x-goog-upload-url": f"https://flow.google.com/upload/v1/flow/upload/video/{PROJECT_ID}?upload_id=u1"},
                        cookies=SimpleNamespace(jar=[]),
                        infos={CurlInfo.COOKIELIST: []},
                    )
                if len(posted_chunks) == 2:
                    # First attempt chunk #1 transiently returns 503 -> triggers session retry!
                    return SimpleNamespace(
                        status_code=503,
                        url=url,
                        headers={},
                        cookies=SimpleNamespace(jar=[]),
                        infos={CurlInfo.COOKIELIST: []},
                    )
                return SimpleNamespace(
                    status_code=200,
                    url=url,
                    headers={},
                    cookies=SimpleNamespace(jar=[]),
                    infos={CurlInfo.COOKIELIST: []},
                    json=lambda: {"mediaId": VIDEO_ID},
                )

        with patch("src.services.flow_current.AsyncSession", _FakeSession), patch(
            "src.services.flow_current.asyncio.sleep", new_callable=AsyncMock
        ):
            media_id = await client.upload_video(
                large_video,
                PROJECT_ID,
                mime_type="video/webm",
                google_cookies="SID=s1; OSID=o1",
            )

        self.assertEqual(media_id, VIDEO_ID)
        # Attempt 1: start + 503 chunk; Attempt 2: start + 8MiB upload + 1MiB+ upload,finalize
        cmds = [(cmd, offset, size) for _, cmd, offset, size in posted_chunks[2:]]
        self.assertEqual(cmds[0][0], "start")
        self.assertEqual(cmds[1], ("upload", "0", 8 * 1024 * 1024))
        self.assertEqual(cmds[2], ("upload, finalize", str(8 * 1024 * 1024), len(large_video) - 8 * 1024 * 1024))

    async def test_wait_for_video_media_ready_survives_transient_poll_glitch(self):
        client = FlowClient(proxy_manager=None)
        client._current_rpc = AsyncMock(
            side_effect=[
                RuntimeError("connection timed out"),
                [VIDEO_ID, PROJECT_ID, "https://flow-content.google/video/ready.mp4"],
            ]
        )
        await client.wait_for_video_media_ready(
            VIDEO_ID, PROJECT_ID, timeout=10, poll_interval=0.5
        )
        self.assertEqual(client._current_rpc.await_count, 2)

    async def test_media_asset_cache_and_material_with_reference_images_and_r2_output(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db = Database(db_path=str(tmp_path / "flow.db"))
            await db.init_db()
            async with db._connect(write=True) as conn:
                await conn.execute(
                    "INSERT INTO tokens (id, st, at, email) VALUES (11, 'st-11', 'at1', 'u1@example.com'), (22, 'st-22', 'at2', 'u2@example.com')"
                )
                await conn.commit()

            flow_client = MagicMock()
            flow_client.upload_video = AsyncMock(return_value=VIDEO_ID)
            flow_client.wait_for_video_media_ready = AsyncMock()
            flow_client.upload_image = AsyncMock(side_effect=[IMG_ID_1, IMG_ID_2, IMG_ID_1, IMG_ID_2])
            flow_client.generate_video_edit = AsyncMock(
                return_value={"operations": [{"operation": {"name": "op-mat-1"}}]}
            )
            flow_client.check_video_status = AsyncMock(
                return_value={
                    "operations": [
                        {
                            "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL",
                            "operation": {
                                "name": "op-mat-1",
                                "metadata": {
                                    "video": {
                                        "fifeUrl": "https://flow-content.google/video/generated.mp4",
                                        "mediaGenerationId": "gen-media-1",
                                    }
                                },
                            },
                        }
                    ]
                }
            )
            flow_client._get_retry_reason = MagicMock(return_value=None)

            handler = GenerationHandler(
                flow_client=flow_client,
                token_manager=_DummyTokenManager(),
                load_balancer=MagicMock(),
                db=db,
                concurrency_manager=_DummyConcurrency(),
                proxy_manager=None,
            )
            handler.file_cache.cache_dir = tmp_path / "cache"
            handler.file_cache.cache_dir.mkdir()
            (handler.file_cache.cache_dir / "dl.mp4").write_bytes(MP4_BYTES)
            handler.file_cache.download_and_cache = AsyncMock(return_value="dl.mp4")

            captured_r2_puts = []

            class _FakeR2Session:
                def __init__(self, **kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return False

                async def put(self, url, headers=None, data=None, **kwargs):
                    captured_r2_puts.append((url, dict(headers or {}), data))
                    return SimpleNamespace(status_code=200, text="")

            env_patch = {
                "R2_ENDPOINT": "https://acct123.r2.cloudflarestorage.com",
                "R2_ACCESS_KEY_ID": "test-ak",
                "R2_SECRET_ACCESS_KEY": "test-sk",
                "R2_BUCKET": "momo-chat-temp",
                "R2_VIDEO_URL_TTL_SECONDS": "86400",
            }
            token1 = SimpleNamespace(
                id=11, at="at1", user_paygate_tier="PAYGATE_TIER_ONE",
                video_concurrency=1, google_cookies="SID=1",
            )
            token2 = SimpleNamespace(
                id=22, at="at2", user_paygate_tier="PAYGATE_TIER_ONE",
                video_concurrency=1, google_cookies="SID=2",
            )

            img1 = PNG_BYTES + b"1"
            img2 = PNG_BYTES + b"2"
            with patch.dict("os.environ", env_patch, clear=False), patch(
                "src.services.r2_storage.AsyncSession", _FakeR2Session
            ), patch.object(type(config), "cache_enabled", False):
                chunks = []
                async for item in handler._handle_video_generation(
                    token=token1,
                    project_id=PROJECT_ID,
                    model_config=MODEL_CONFIG["omni-1.2-flash-8s-landscape"],
                    prompt="apply reference character to video",
                    images=[img1, img2],
                    stream=False,
                    resolution="360p",
                    source_video=WEBM_BYTES,
                    source_start_frame=0,
                    source_end_frame=180,
                ):
                    chunks.append(item)

            # Verify generate_video_edit received both reference images + WebM upload metadata
            edit_kwargs = flow_client.generate_video_edit.await_args.kwargs
            self.assertEqual(edit_kwargs["source_media_id"], VIDEO_ID)
            self.assertEqual(edit_kwargs["reference_media_ids"], [IMG_ID_1, IMG_ID_2])
            self.assertEqual(flow_client.upload_video.await_args.kwargs["mime_type"], "video/webm")

            # Verify R2 PUT & 24h signed URL stored cleanly and temp file unlinked
            self.assertEqual(len(captured_r2_puts), 1)
            put_url, put_headers, _ = captured_r2_puts[0]
            self.assertIn("/momo-chat-temp/video-temp/out/", put_url)
            self.assertEqual(put_headers["Cache-Control"], "private, max-age=86400")
            self.assertFalse((handler.file_cache.cache_dir / "dl.mp4").exists())
            parsed_res = json.loads(chunks[-1])
            final_url = parsed_res["url"]
            self.assertIn("https://acct123.r2.cloudflarestorage.com/momo-chat-temp/video-temp/out/", final_url)
            self.assertEqual(parse_qs(urlparse(final_url).query)["X-Amz-Expires"], ["86400"])

            # Verify same (token1, project_id) reuses cached video & images without re-uploading!
            flow_client.upload_video.reset_mock()
            flow_client.upload_image.reset_mock()
            vid_id_again, cached_flag, _ = await handler._upload_source_video_with_cache(
                token1, PROJECT_ID, WEBM_BYTES
            )
            self.assertEqual((vid_id_again, cached_flag), (VIDEO_ID, True))
            flow_client.upload_video.assert_not_awaited()

            # Verify different token (failover to token2) misses cache and re-uploads!
            vid_id_t2, cached_t2, _ = await handler._upload_source_video_with_cache(
                token2, PROJECT_ID, WEBM_BYTES
            )
            self.assertEqual((vid_id_t2, cached_t2), (VIDEO_ID, False))
            flow_client.upload_video.assert_awaited_once()

    async def test_v1_videos_async_r2_url_download_and_mixed_material_validation(self):
        config.api_key = "secret-key"
        recorded_tasks = []

        async def _fake_collect(*args, **kwargs):
            recorded_tasks.append((args, kwargs))
            r2_url = "https://acct123.r2.cloudflarestorage.com/momo-chat-temp/video-temp/out/final.mp4?X-Amz-Expires=86400"
            return json.dumps({
                "url": r2_url,
                "choices": [{"message": {"content": f"<video src='{r2_url}' controls></video>"}}],
            })

        async def _fake_download_video(url):
            return WEBM_BYTES

        async def _fake_retrieve_image(url):
            return PNG_BYTES

        fake_handler = SimpleNamespace(
            db=None,
            file_cache=SimpleNamespace(cache_dir=Path("."), download_and_cache=AsyncMock()),
        )
        with patch.object(routes, "generation_handler", fake_handler), patch.object(
            routes, "_is_safe_remote_media_url", return_value=True
        ), patch.object(
            routes, "_download_remote_video_bytes", AsyncMock(side_effect=_fake_download_video)
        ) as dl_vid, patch.object(
            routes, "retrieve_image_data", AsyncMock(side_effect=_fake_retrieve_image)
        ) as dl_img, patch.object(
            routes, "_collect_non_stream_result", AsyncMock(side_effect=_fake_collect)
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/v1/videos",
                    headers={"Authorization": "Bearer secret-key"},
                    json={
                        "model": "omni-1.2-flash",
                        "prompt": "mix video with character refs",
                        "resolution": "360p",
                        "source_start_frame": 0,
                        "source_end_frame": 120,
                        "reference_video": "https://acct123.r2.cloudflarestorage.com/momo-chat-temp/video-temp/in/clip.webm?X-Amz-Expires=86400",
                        "input_reference": [
                            "https://acct123.r2.cloudflarestorage.com/momo-chat-temp/video-temp/in/ref1.png",
                            "https://acct123.r2.cloudflarestorage.com/momo-chat-temp/video-temp/in/ref2.png",
                        ],
                    },
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                task_id = resp.json()["id"]
                bg_task = routes._background_video_tasks.get(task_id)
                if bg_task is not None:
                    await bg_task

                # Check background task resolved WebM bytes + 2 ref images and completed with R2 URL
                self.assertEqual(dl_vid.await_count, 1)
                self.assertEqual(dl_img.await_count, 2)
                self.assertEqual(len(recorded_tasks), 1)
                self.assertEqual(recorded_tasks[0][1]["source_video"], WEBM_BYTES)
                self.assertEqual(len(recorded_tasks[0][0][2]), 2)

                status_resp = await client.get(
                    f"/v1/videos/{task_id}",
                    headers={"Authorization": "Bearer secret-key"},
                )
                self.assertEqual(status_resp.status_code, 200)
                status_json = status_resp.json()
                self.assertEqual(status_json.get("status"), "completed", status_json)
                self.assertIn("r2.cloudflarestorage.com", status_json["url"])

                # Check /content redirects directly to R2 without calling file_cache.download_and_cache
                content_resp = await client.get(
                    f"/v1/videos/{task_id}/content",
                    headers={"Authorization": "Bearer secret-key"},
                    follow_redirects=False,
                )
                self.assertEqual(content_resp.status_code, 302)
                self.assertIn("r2.cloudflarestorage.com", content_resp.headers["location"])
                fake_handler.file_cache.download_and_cache.assert_not_awaited()

                # Reject > 3 images with reference_video
                too_many = await client.post(
                    "/v1/videos",
                    headers={"Authorization": "Bearer secret-key"},
                    json={
                        "model": "omni-1.2-flash",
                        "prompt": "too many",
                        "resolution": "360p",
                        "source_end_frame": 120,
                        "reference_video": "https://acct123.r2.cloudflarestorage.com/momo-chat-temp/video-temp/in/clip.webm",
                        "input_reference": ["AAAA", "BBBB", "CCCC", "DDDD"],
                    },
                )
                self.assertEqual(too_many.status_code, 400)
                self.assertIn("at most 3", too_many.text)

        # Reject unsafe SSRF URL
        with patch.object(routes, "generation_handler", fake_handler):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                unsafe = await client.post(
                    "/v1/videos",
                    headers={"Authorization": "Bearer secret-key"},
                    json={
                        "model": "omni-1.2-flash",
                        "prompt": "ssrf",
                        "resolution": "360p",
                        "source_end_frame": 120,
                        "reference_video": "http://127.0.0.1/internal.mp4",
                    },
                )
                self.assertEqual(unsafe.status_code, 400)
