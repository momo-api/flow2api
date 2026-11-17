"""Cloudflare R2 S3v4 temporary video output storage for Flow2API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import os
from pathlib import Path
import time
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse
import uuid

from curl_cffi.requests import AsyncSession

from ..core.logger import debug_logger

DEFAULT_R2_VIDEO_PREFIX = "video-temp/out"
DEFAULT_R2_VIDEO_TTL_SECONDS = 86400
MAX_R2_VIDEO_TTL_SECONDS = 86400
MIN_R2_VIDEO_TTL_SECONDS = 60
_R2_UPLOAD_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class _NonRetryableR2Error(RuntimeError):
    """Raised when R2 rejects the upload with a non-retryable 4xx response."""


def _clean_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if isinstance(value, str) and value.strip() else default


def _clamp_ttl_seconds(raw: Any) -> int:
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_R2_VIDEO_TTL_SECONDS
    return max(MIN_R2_VIDEO_TTL_SECONDS, min(parsed, MAX_R2_VIDEO_TTL_SECONDS))


def get_r2_video_config() -> Optional[Dict[str, Any]]:
    """Read R2 S3 configuration from environment variables, or return None if incomplete."""
    access_key_id = _clean_env("R2_ACCESS_KEY_ID")
    secret_access_key = _clean_env("R2_SECRET_ACCESS_KEY")
    account_id = _clean_env("R2_ACCOUNT_ID")
    endpoint = _clean_env("R2_ENDPOINT")
    if not endpoint and account_id:
        endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    bucket = _clean_env("R2_BUCKET", "momo-chat-temp")
    if not (endpoint and access_key_id and secret_access_key and bucket):
        return None
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    prefix = _clean_env("R2_VIDEO_OUTPUT_PREFIX", DEFAULT_R2_VIDEO_PREFIX).strip("/")
    if not prefix or ".." in prefix.split("/"):
        prefix = DEFAULT_R2_VIDEO_PREFIX
    ttl_seconds = _clamp_ttl_seconds(
        _clean_env("R2_VIDEO_URL_TTL_SECONDS", str(DEFAULT_R2_VIDEO_TTL_SECONDS))
    )
    return {
        "endpoint": f"https://{parsed.netloc}",
        "host": parsed.netloc,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "bucket": bucket,
        "prefix": prefix,
        "ttl_seconds": ttl_seconds,
    }


def is_r2_video_storage_configured() -> bool:
    return get_r2_video_config() is not None


def is_r2_storage_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    hostname = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and hostname.endswith(".r2.cloudflarestorage.com")
        and not any(c in hostname for c in "\\\r\n")
    )


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_access_key: str, date_stamp: str, region: str = "auto", service: str = "s3") -> bytes:
    k_date = _hmac_sha256(("AWS4" + secret_access_key).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "aws4_request")


def _encode_s3_path(bucket: str, object_key: str) -> str:
    safe_parts = [p for p in object_key.strip("/").split("/") if p and p not in {".", ".."}]
    if not safe_parts:
        raise ValueError("Invalid R2 object key")
    joined = "/".join(quote(part, safe="-_.~") for part in safe_parts)
    return f"/{quote(bucket, safe='-_.~')}/{joined}"


def build_presigned_get_url(
    config_dict: Dict[str, Any],
    object_key: str,
    *,
    expires_in: Optional[int] = None,
    now: Optional[datetime] = None,
) -> str:
    """Create a Cloudflare R2 SigV4 presigned GET URL valid for up to 24 hours."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = moment.strftime("%Y%m%d")
    ttl = _clamp_ttl_seconds(expires_in if expires_in is not None else config_dict["ttl_seconds"])
    credential_scope = f"{date_stamp}/auto/s3/aws4_request"
    canonical_uri = _encode_s3_path(config_dict["bucket"], object_key)
    query_items = [
        ("X-Amz-Algorithm", "AWS4-HMAC-SHA256"),
        ("X-Amz-Credential", f"{config_dict['access_key_id']}/{credential_scope}"),
        ("X-Amz-Date", amz_date),
        ("X-Amz-Expires", str(ttl)),
        ("X-Amz-SignedHeaders", "host"),
    ]
    canonical_query = "&".join(
        f"{quote(k, safe='')}={quote(v, safe='-_.~')}" for k, v in sorted(query_items)
    )
    canonical_headers = f"host:{config_dict['host']}\n"
    canonical_request = "\n".join(
        [
            "GET",
            canonical_uri,
            canonical_query,
            canonical_headers,
            "host",
            "UNSIGNED-PAYLOAD",
        ]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    key = _signing_key(config_dict["secret_access_key"], date_stamp)
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{config_dict['endpoint']}{canonical_uri}?{canonical_query}&X-Amz-Signature={signature}"


def _build_signed_put_headers(
    config_dict: Dict[str, Any],
    canonical_uri: str,
    payload_sha256: str,
    content_type: str,
    cache_control: str,
    now: datetime,
) -> Dict[str, str]:
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    credential_scope = f"{date_stamp}/auto/s3/aws4_request"
    headers_to_sign = {
        "cache-control": cache_control,
        "content-type": content_type,
        "host": config_dict["host"],
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
    }
    signed_headers = ";".join(sorted(headers_to_sign))
    canonical_headers = "".join(f"{k}:{headers_to_sign[k]}\n" for k in sorted(headers_to_sign))
    canonical_request = "\n".join(
        [
            "PUT",
            canonical_uri,
            "",
            canonical_headers,
            signed_headers,
            payload_sha256,
        ]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signing_key = _signing_key(config_dict["secret_access_key"], date_stamp)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={config_dict['access_key_id']}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Authorization": authorization,
        "Cache-Control": cache_control,
        "Content-Type": content_type,
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
    }


def _safe_object_key(prefix: str, task_id: str = "", ext: str = "mp4") -> str:
    date_segment = time.strftime("%Y/%m/%d", time.gmtime())
    safe_task = "".join(c for c in str(task_id or "") if c.isalnum() or c in "-_")[:48]
    stem = f"{safe_task}-{uuid.uuid4().hex[:12]}" if safe_task else uuid.uuid4().hex
    clean_ext = "".join(c for c in str(ext or "mp4").lower() if c.isalnum()) or "mp4"
    return f"{prefix}/{date_segment}/{stem}.{clean_ext}"


async def upload_video_bytes(
    video_bytes: bytes,
    *,
    task_id: str = "",
    content_type: str = "video/mp4",
    ext: str = "mp4",
    expires_in: Optional[int] = None,
    timeout: int = 120,
    max_attempts: int = 3,
) -> Optional[Dict[str, Any]]:
    """Upload MP4 bytes directly to Cloudflare R2 and return a 24h presigned GET URL."""
    cfg = get_r2_video_config()
    if cfg is None or not video_bytes:
        return None

    object_key = _safe_object_key(cfg["prefix"], task_id=task_id, ext=ext)
    canonical_uri = _encode_s3_path(cfg["bucket"], object_key)
    url = f"{cfg['endpoint']}{canonical_uri}"
    payload_sha256 = hashlib.sha256(video_bytes).hexdigest()
    ttl = _clamp_ttl_seconds(expires_in if expires_in is not None else cfg["ttl_seconds"])
    cache_control = f"private, max-age={ttl}"

    last_error: Optional[Exception] = None
    attempts = max(1, int(max_attempts))
    for attempt in range(attempts):
        now = datetime.now(timezone.utc)
        headers = _build_signed_put_headers(
            cfg,
            canonical_uri=canonical_uri,
            payload_sha256=payload_sha256,
            content_type=content_type,
            cache_control=cache_control,
            now=now,
        )
        try:
            async with AsyncSession(trust_env=False) as session:
                response = await session.put(
                    url,
                    headers=headers,
                    data=video_bytes,
                    timeout=max(30, int(timeout)),
                    impersonate="chrome",
                )
            if 200 <= response.status_code < 300:
                signed_url = build_presigned_get_url(
                    cfg, object_key, expires_in=ttl, now=now
                )
                return {
                    "bucket": cfg["bucket"],
                    "object_key": object_key,
                    "url": signed_url,
                    "expires_in": ttl,
                }
            msg = f"R2 video upload failed: HTTP {response.status_code} {(response.text or '')[:160]}"
            if response.status_code not in _R2_UPLOAD_RETRYABLE_STATUS:
                raise _NonRetryableR2Error(msg)
            last_error = RuntimeError(msg)
        except _NonRetryableR2Error:
            raise
        except Exception as exc:
            last_error = exc
        if attempt < attempts - 1:
            await asyncio.sleep(min(2 ** attempt, 4))

    debug_logger.log_warning(f"[R2] 视频上传 R2 失败: {last_error}")
    if last_error:
        raise last_error
    return None


async def upload_video_file(
    file_path: Path,
    *,
    task_id: str = "",
    content_type: str = "video/mp4",
    ext: str = "mp4",
    expires_in: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    data = Path(file_path).read_bytes()
    return await upload_video_bytes(
        data,
        task_id=task_id,
        content_type=content_type,
        ext=ext,
        expires_in=expires_in,
    )
