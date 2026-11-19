from datetime import datetime, timezone
import hashlib
import hmac
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from src.services import r2_storage


class R2MediaGatewayUrlTests(unittest.TestCase):
    def _base_env(self):
        return {
            "R2_ACCOUNT_ID": "acct123",
            "R2_BUCKET": "momo-chat-temp",
            "R2_ACCESS_KEY_ID": "access-key",
            "R2_SECRET_ACCESS_KEY": "secret-key",
        }

    def test_build_presigned_get_url_uses_gateway_when_configured(self):
        env = {
            **self._base_env(),
            "R2_MEDIA_GATEWAY_BASE_URL": "https://media.momoapi.us/",
            "R2_MEDIA_SIGNING_SECRET": "test-signing-secret",
        }
        now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
        with patch.dict(os.environ, env, clear=False):
            cfg = r2_storage.get_r2_video_config()
            self.assertIsNotNone(cfg)
            url = r2_storage.build_presigned_get_url(
                cfg,
                "video-temp/out/2026/11/19/sample.mp4",
                expires_in=3600,
                now=now,
            )
        parsed = urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "media.momoapi.us")
        self.assertEqual(parsed.path, "/video-temp/out/2026/11/19/sample.mp4")
        qs = parse_qs(parsed.query)
        expected_exp = str(int(now.timestamp()) + 3600)
        self.assertEqual(qs["exp"], [expected_exp])
        expected_sig = hmac.new(
            b"test-signing-secret",
            f"GET\n/video-temp/out/2026/11/19/sample.mp4\n{expected_exp}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(qs["sig"], [expected_sig])

    def test_build_presigned_get_url_falls_back_to_s3_sigv4_when_unset(self):
        with patch.dict(
            os.environ,
            {
                **self._base_env(),
                "R2_MEDIA_GATEWAY_BASE_URL": "",
                "R2_MEDIA_SIGNING_SECRET": "",
            },
            clear=False,
        ):
            cfg = r2_storage.get_r2_video_config()
            url = r2_storage.build_presigned_get_url(
                cfg,
                "video-temp/out/2026/11/19/sample.mp4",
            )
        parsed = urlparse(url)
        self.assertEqual(parsed.netloc, "acct123.r2.cloudflarestorage.com")
        self.assertIn("X-Amz-Signature=", parsed.query)

    def test_is_r2_storage_url_recognizes_s3_and_gateway_hosts(self):
        self.assertTrue(r2_storage.is_r2_storage_url("https://acct123.r2.cloudflarestorage.com/momo-chat-temp/a.mp4"))
        self.assertTrue(r2_storage.is_r2_storage_url("https://media.momoapi.us/video-temp/in/clip.mp4?exp=1&sig=a"))
        with patch.dict(os.environ, {"R2_MEDIA_GATEWAY_BASE_URL": "https://cdn-media.example.com"}, clear=False):
            self.assertTrue(r2_storage.is_r2_storage_url("https://cdn-media.example.com/video-temp/in/clip.mp4"))
            self.assertFalse(r2_storage.is_r2_storage_url("https://evil.example.com/video-temp/in/clip.mp4"))


if __name__ == "__main__":
    unittest.main()
