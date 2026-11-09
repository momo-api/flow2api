"""Flow API Client for VideoFX (Veo)"""

import asyncio
import json
import contextvars
import time
import uuid
import random
import base64
import gzip
import ssl
import re
from typing import Dict, Any, Optional, List, Union, Callable, Awaitable
from urllib.parse import quote, urljoin, urlparse
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from curl_cffi import CurlInfo, CurlOpt
from curl_cffi.requests import AsyncSession
from ..core.logger import debug_logger
from ..core.config import (
    config,
    get_yescaptcha_min_score,
    is_yes_style_captcha_method,
)
from .flow_current import CurrentFlowClientMixin
from .flow_frontend import FlowFrontendMixin
from .flow_cookies import FlowCookieJar

try:
    import httpx
except ImportError:
    httpx = None


def _safe_updated_flow_cookies(owner, original) -> str:
    """读取 Cookie 最新快照，缺失实现时安全降级。

    历史报错：'FlowClient' object has no attribute 'get_updated_flow_cookies'
    成因是自定义 flow_frontend 覆盖了上游新增的 Cookie 作用域方法。
    """
    getter = getattr(owner, "get_updated_flow_cookies", None)
    if getter is None:
        return str(original or "").strip()
    try:
        return getter(original)
    except Exception:
        return str(original or "").strip()


class _FlowClientBase(FlowFrontendMixin):
    """VideoFX API客户端"""

    FLOW_PUBLIC_API_KEY = "AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY"
    FLOW_BROWSER_CHANNEL_HEADER = "stable"
    FLOW_BROWSER_COPYRIGHT_HEADER = "Copyright 2026 Google LLC. All Rights Reserved."
    FLOW_BROWSER_VALIDATION_HEADER = "MRCPrt/rS3JY47x2Yiz9h3ag4U8="
    FLOW_BROWSER_YEAR_HEADER = "2026"
    FLOW_FRONTEND_EXPERIMENT_IDS = (
        "106184493,106256669,105798603,106281924,106259075,106262194,"
        "105993823,106104244,105484652,1714252,105928947,106238955,"
        "106225453,106131447,1706538,119157484,106243706,105746691,"
        "106151974,106125249,106001691,106077941"
    )
    YESCAPTCHA_SLOT_BACKOFF_SECONDS = (3, 5, 8, 13, 20)

    def __init__(self, proxy_manager, db=None):
        self.proxy_manager = proxy_manager
        self.db = db  # Database instance for captcha config
        self.labs_base_url = config.flow_labs_base_url
        self.api_base_url = config.flow_api_base_url
        self.timeout = config.flow_timeout
        # 缓存每个账号的 User-Agent
        self._user_agent_cache = {}
        # 当前请求链路绑定的浏览器指纹（基于 contextvar，避免并发串扰）
        self._request_fingerprint_ctx: contextvars.ContextVar[
            Optional[Dict[str, Any]]
        ] = contextvars.ContextVar("flow_request_fingerprint", default=None)
        self._remote_browser_prefill_last_sent: Dict[str, float] = {}

        # 仅保留当前上游仍稳定出现的最小浏览器风格头；具体 UA / Accept-Language / UA-CH
        # 统一以当前请求链路绑定的 runtime fingerprint 为准，不再兼容旧版随机平台策略。
        self._default_client_headers = {
            "sec-ch-ua-mobile": "?0",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
        }
        # 发车策略改为“请求到就发”：
        # 不在 flow2api 本地对提交做批次整形或排队，避免把同批请求打成阶梯。

    def _generate_user_agent(self, account_id: str = None) -> str:
        """基于账号ID生成固定的 User-Agent
        
        Args:
            account_id: 账号标识（如 email 或 token_id），相同账号返回相同 UA
            
        Returns:
            User-Agent 字符串
        """
        if not hasattr(self, "_user_agent_cache"):
            self._user_agent_cache = {}

        # 如果没有提供账号ID，生成随机UA
        if not account_id:
            account_id = f"random_{random.randint(1, 999999)}"
        
        # 如果已缓存，直接返回
        if account_id in self._user_agent_cache:
            return self._user_agent_cache[account_id]
        
        # 使用账号ID作为随机种子，确保同一账号生成相同的UA
        import hashlib

        seed = int(hashlib.md5(account_id.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        
        # fallback 仅在没有 runtime fingerprint 时兜底，直接与当前上游 Windows Chrome 风格对齐
        chrome_versions = ["136.0.0.0"]
        ch_version = rng.choice(chrome_versions)
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ch_version} Safari/537.36"
        )
        
        # 缓存结果
        self._user_agent_cache[account_id] = user_agent
        
        return user_agent

    def _set_request_fingerprint(self, fingerprint: Optional[Dict[str, Any]]):
        """设置当前请求链路的浏览器指纹上下文。"""
        if not hasattr(self, "_request_fingerprint_ctx"):
            self._request_fingerprint_ctx = contextvars.ContextVar(
                "flow_request_fingerprint",
                default=None,
            )
        self._request_fingerprint_ctx.set(dict(fingerprint) if fingerprint else None)

    def get_request_fingerprint(self) -> Optional[Dict[str, Any]]:
        """获取当前请求链路绑定的浏览器指纹快照。"""
        if not hasattr(self, "_request_fingerprint_ctx"):
            return None
        fingerprint = self._request_fingerprint_ctx.get()
        if not isinstance(fingerprint, dict) or not fingerprint:
            return None
        return dict(fingerprint)

    def clear_request_fingerprint(self):
        """清理请求链路绑定的浏览器指纹。"""
        self._set_request_fingerprint(None)

    def _set_captcha_request_context(self, context: Optional[Dict[str, Any]]):
        if not hasattr(self, "_captcha_request_ctx"):
            self._captcha_request_ctx = contextvars.ContextVar("flow_captcha_request", default=None)
        self._captcha_request_ctx.set(dict(context) if context else None)

    def _get_captcha_request_context(self) -> Dict[str, Any]:
        context = self._captcha_request_ctx.get() if hasattr(self, "_captcha_request_ctx") else None
        return dict(context) if context else {}

    def _current_browser_ref(self, token_id: Optional[int]):
        context = self._get_captcha_request_context()
        if context.get("method") == "browser" and context.get("token_id") == token_id:
            return context.get("browser_ref")
        return None

    def _get_primary_accept_language(self, fallback: str = "zh-CN,zh;q=0.9") -> str:
        fingerprint = self.get_request_fingerprint()
        if isinstance(fingerprint, dict):
            accept_language = str(fingerprint.get("accept_language") or "").strip()
            if accept_language:
                return self._normalize_accept_language_header(
                    accept_language, fallback=fallback
                )
        return self._normalize_accept_language_header(fallback, fallback=fallback)

    def _get_primary_locale_code(self, fallback: str = "en-US") -> str:
        fingerprint = self.get_request_fingerprint()
        if isinstance(fingerprint, dict):
            language = str(fingerprint.get("language") or "").strip()
            if language:
                return language
            accept_language = str(fingerprint.get("accept_language") or "").strip()
            if accept_language:
                primary = accept_language.split(",", 1)[0].strip()
                if primary:
                    return primary
        return fallback

    def _infer_sec_ch_ua_from_user_agent(self, user_agent: Optional[str]) -> str:
        ua = str(user_agent or "").strip()
        if not ua:
            return ""
        import re

        match = re.search(r"(?:Chrome|Chromium)/(\d+)", ua, re.IGNORECASE)
        major = match.group(1) if match else "124"
        return (
            f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not)A;Brand";v="24"'
        )

    def _normalize_sec_ch_ua_header(
        self,
        sec_ch_ua: Optional[str],
        *,
        user_agent: Optional[str] = None,
    ) -> str:
        raw = str(sec_ch_ua or "").strip()
        inferred = self._infer_sec_ch_ua_from_user_agent(user_agent)
        if not raw:
            return inferred
        ua_text = str(user_agent or "").lower()
        if "chrome/" in ua_text and "google chrome" not in raw.lower():
            return inferred
        return raw or inferred

    def _build_fingerprint_from_user_agent(
        self,
        user_agent: Optional[str],
        *,
        accept_language: Optional[str] = None,
        proxy_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        ua = str(user_agent or "").strip()
        if not ua:
            return {}
        ua_lower = ua.lower()
        platform = '"Windows"'
        mobile = "?0"
        if "android" in ua_lower:
            platform = '"Android"'
            mobile = "?1"
        elif "iphone" in ua_lower or "ipad" in ua_lower or "ios" in ua_lower:
            platform = '"iOS"'
            mobile = "?1"
        elif "mac" in ua_lower:
            platform = '"macOS"'
        elif "linux" in ua_lower or "x11" in ua_lower:
            platform = '"Linux"'
        fingerprint: Dict[str, Any] = {
            "user_agent": ua,
            "sec_ch_ua": self._infer_sec_ch_ua_from_user_agent(ua),
            "sec_ch_ua_mobile": mobile,
            "sec_ch_ua_platform": platform,
        }
        normalized_accept_language = self._normalize_accept_language_header(
            accept_language
        )
        if normalized_accept_language:
            fingerprint["accept_language"] = normalized_accept_language
        if proxy_url:
            fingerprint["proxy_url"] = proxy_url
        return fingerprint

    def _normalize_accept_language_header(
        self,
        accept_language: Optional[str],
        fallback: str = "zh-CN,zh;q=0.9",
    ) -> str:
        raw = str(accept_language or "").strip()
        if not raw:
            return fallback
        if "," in raw:
            normalized_parts: list[str] = []
            for index, item in enumerate(raw.split(",")):
                candidate = str(item or "").strip()
                if not candidate:
                    continue
                language = candidate.split(";", 1)[0].strip()
                if not language:
                    continue
                if index == 0:
                    normalized_parts.append(language)
                    continue
                q_match = re.search(r";\s*q=([0-9.]+)", candidate, re.IGNORECASE)
                q_value = (
                    q_match.group(1)
                    if q_match
                    else f"{max(0.1, 1 - (index * 0.1)):.1f}"
                )
                normalized_parts.append(f"{language};q={q_value}")
            if normalized_parts:
                return ",".join(normalized_parts)
            return fallback
        if "-" in raw:
            primary = raw.split("-", 1)[0].strip()
            if len(primary) == 2 and primary.isalpha():
                return f"{raw},{primary};q=0.9"
        return raw

    def _get_effective_request_user_agent(
        self, account_id: Optional[str] = None
    ) -> str:
        fingerprint = self.get_request_fingerprint()
        if isinstance(fingerprint, dict):
            user_agent = str(fingerprint.get("user_agent") or "").strip()
            if user_agent:
                return user_agent
        return self._generate_user_agent(account_id)

    @staticmethod
    def _should_attach_runtime_session_cookies(url: str) -> bool:
        host = str(urlparse(str(url or "")).hostname or "").lower()
        if not host:
            return False
        return any(
            host == candidate or host.endswith(f".{candidate}")
            for candidate in (
                "google.com",
                "googleapis.com",
                "recaptcha.net",
            )
        )

    @staticmethod
    def _merge_cookie_header(
        existing_cookie_header: Optional[str],
        extra_cookies: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        cookie_items: Dict[str, str] = {}
        raw_existing = str(existing_cookie_header or "").strip()
        if raw_existing:
            for part in raw_existing.split(";"):
                item = str(part or "").strip()
                if not item or "=" not in item:
                    continue
                key, value = item.split("=", 1)
                key = str(key or "").strip()
                value = str(value or "").strip()
                if key:
                    cookie_items[key] = value

        if isinstance(extra_cookies, dict):
            for key, value in extra_cookies.items():
                normalized_key = str(key or "").strip()
                normalized_value = str(value or "").strip()
                if (
                    normalized_key
                    and normalized_value
                    and normalized_key not in cookie_items
                ):
                    cookie_items[normalized_key] = normalized_value

        if not cookie_items:
            return raw_existing or None
        return "; ".join(f"{key}={value}" for key, value in cookie_items.items())

    def _build_flow_project_page_url(self, project_id: str) -> str:
        return f"https://flow.google.com/project/{project_id}"

    def _build_current_flow_media_headers(
        self,
        *,
        content_type: str = "application/json",
    ) -> Dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": self._get_primary_accept_language(
                fallback="zh-CN,zh;q=0.9"
            ),
            "Content-Type": content_type,
            "Origin": "https://flow.google.com",
            "Priority": "u=1, i",
            "Referer": "https://flow.google.com/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "sec-fetch-storage-access": "active",
        }
        headers.setdefault("x-browser-channel", self.FLOW_BROWSER_CHANNEL_HEADER)
        headers.setdefault("x-browser-copyright", self.FLOW_BROWSER_COPYRIGHT_HEADER)
        headers.setdefault("x-browser-validation", self.FLOW_BROWSER_VALIDATION_HEADER)
        headers.setdefault("x-browser-year", self.FLOW_BROWSER_YEAR_HEADER)
        return headers

    def _build_labs_request_context_headers(
        self, project_id: Optional[str]
    ) -> Dict[str, str]:
        return self._build_current_flow_media_headers()

    def _compact_json_dumps(self, payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _encode_trpc_input(self, payload: Dict[str, Any]) -> str:
        return quote(self._compact_json_dumps(payload), safe="")

    @staticmethod
    def _extract_project_id_from_request_payload(
        payload: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        client_context = payload.get("clientContext")
        if isinstance(client_context, dict):
            project_id = str(client_context.get("projectId") or "").strip()
            if project_id:
                return project_id
        requests = payload.get("requests")
        if isinstance(requests, list):
            for item in requests:
                if not isinstance(item, dict):
                    continue
                item_client_context = item.get("clientContext")
                if not isinstance(item_client_context, dict):
                    continue
                project_id = str(item_client_context.get("projectId") or "").strip()
                if project_id:
                    return project_id
        return None

    async def _get_token_st_by_id(self, token_id: Optional[int]) -> Optional[str]:
        if not token_id or self.db is None or not hasattr(self.db, "get_token"):
            return None
        try:
            token = await self.db.get_token(int(token_id))
            st_value = str(getattr(token, "st", "") or "").strip() if token else ""
            return st_value or None
        except Exception as e:
            debug_logger.log_warning(
                f"[VIDEO WARMUP] 读取 Token-{token_id} 的 ST 失败: {e}"
            )
            return None

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        raw_body: Optional[Union[str, bytes]] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None,
        timeout: Optional[int] = None,
        use_media_proxy: bool = False,
        respect_fingerprint_proxy: bool = True,
        force_no_proxy: bool = False,
        allow_urllib_fallback: bool = True,
        apply_default_client_headers: bool = True,
        impersonate: str = "chrome",
    ) -> Dict[str, Any]:
        """统一HTTP请求处理"""
        fingerprint = self._request_fingerprint_ctx.get()

        proxy_url = None
        if not force_no_proxy:
            if self.proxy_manager:
                if use_media_proxy and hasattr(
                    self.proxy_manager, "get_media_proxy_url"
                ):
                    proxy_url = await self.proxy_manager.get_media_proxy_url()
                elif hasattr(self.proxy_manager, "get_request_proxy_url"):
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                else:
                    proxy_url = await self.proxy_manager.get_proxy_url()

            if (
                respect_fingerprint_proxy
                and isinstance(fingerprint, dict)
                and "proxy_url" in fingerprint
            ):
                proxy_url = fingerprint.get("proxy_url")
                if proxy_url == "":
                    proxy_url = None
        request_timeout = timeout or self.timeout

        if headers is None:
            headers = {}
        else:
            headers = dict(headers)

        if use_st and st_token:
            headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"

        if use_at and at_token:
            headers["authorization"] = f"Bearer {at_token}"

        account_id = None
        if st_token:
            account_id = st_token[:16]
        elif at_token:
            account_id = at_token[:16]

        fingerprint_user_agent = None
        if isinstance(fingerprint, dict):
            fingerprint_user_agent = fingerprint.get("user_agent")

        effective_user_agent = str(
            fingerprint_user_agent or self._generate_user_agent(account_id)
        ).strip()
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("User-Agent", effective_user_agent)
        headers.setdefault(
            "Accept-Language",
            self._get_primary_accept_language(fallback="zh-CN,zh;q=0.9"),
        )

        if isinstance(fingerprint, dict):
            if fingerprint.get("accept_language"):
                headers.setdefault("Accept-Language", fingerprint["accept_language"])
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = self._normalize_sec_ch_ua_header(
                    fingerprint["sec_ch_ua"],
                    user_agent=headers.get("User-Agent"),
                )
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = fingerprint["sec_ch_ua_mobile"]
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = fingerprint["sec_ch_ua_platform"]

        if apply_default_client_headers:
            for key, value in self._default_client_headers.items():
                headers.setdefault(key, value)

        inferred_fingerprint = self._build_fingerprint_from_user_agent(
            headers.get("User-Agent"),
            accept_language=headers.get("Accept-Language"),
            proxy_url=proxy_url,
        )
        if not headers.get("sec-ch-ua") and inferred_fingerprint.get("sec_ch_ua"):
            headers["sec-ch-ua"] = inferred_fingerprint["sec_ch_ua"]
        if not headers.get("sec-ch-ua-platform") and inferred_fingerprint.get(
            "sec_ch_ua_platform"
        ):
            headers["sec-ch-ua-platform"] = inferred_fingerprint["sec_ch_ua_platform"]
        if not headers.get("sec-ch-ua-mobile") and inferred_fingerprint.get(
            "sec_ch_ua_mobile"
        ):
            headers["sec-ch-ua-mobile"] = inferred_fingerprint["sec_ch_ua_mobile"]

        if isinstance(
            fingerprint, dict
        ) and self._should_attach_runtime_session_cookies(url):
            origin = (
                str(fingerprint.get("origin") or "").strip()
                or "https://flow.google.com"
            )
            referer = str(fingerprint.get("referer") or "").strip()
            if not referer:
                fingerprint_project_id = str(
                    fingerprint.get("project_id") or ""
                ).strip()
                if fingerprint_project_id:
                    referer = self._build_flow_project_page_url(fingerprint_project_id)
            if origin:
                headers.setdefault("Origin", origin)
            if referer:
                headers.setdefault("Referer", referer)
            merged_cookie_header = self._merge_cookie_header(
                headers.get("Cookie"),
                fingerprint.get("session_cookies"),
            )
            if merged_cookie_header:
                headers["Cookie"] = merged_cookie_header

        if self._should_attach_runtime_session_cookies(url):
            derived_project_id = self._extract_project_id_from_request_payload(
                json_data
            )
            headers.setdefault("Origin", "https://flow.google.com")
            if derived_project_id:
                headers.setdefault(
                    "Referer", self._build_flow_project_page_url(derived_project_id)
                )

        request_body_for_log = raw_body if raw_body is not None else json_data
        if config.debug_enabled:
            if isinstance(fingerprint, dict):
                proxy_for_log = proxy_url if proxy_url else "direct"
                debug_logger.log_info(
                    f"[FINGERPRINT] 使用打码浏览器指纹提交请求: UA={headers.get('User-Agent', '')[:120]}, proxy={proxy_for_log}"
                )
            debug_logger.log_request(
                method=method,
                url=url,
                headers=headers,
                body=request_body_for_log,
                proxy=proxy_url,
            )

        start_time = time.time()

        try:
            async with AsyncSession(trust_env=False) as session:
                if method.upper() == "GET":
                    response = await session.get(
                        url,
                        headers=headers,
                        proxy=proxy_url,
                        timeout=request_timeout,
                        impersonate=impersonate,
                    )
                else:
                    request_kwargs = {
                        "headers": headers,
                        "proxy": proxy_url,
                        "timeout": request_timeout,
                        "impersonate": impersonate,
                    }
                    if raw_body is not None:
                        request_kwargs["data"] = raw_body
                    else:
                        request_kwargs["json"] = json_data
                    response = await session.post(url, **request_kwargs)

                duration_ms = (time.time() - start_time) * 1000

                if config.debug_enabled:
                    debug_logger.log_response(
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        body=response.text,
                        duration_ms=duration_ms,
                    )

                if response.status_code >= 400:
                    error_reason = f"HTTP Error {response.status_code}"
                    try:
                        error_body = response.json()
                        if "error" in error_body:
                            error_info = error_body["error"]
                            error_message = error_info.get("message", "")
                            details = error_info.get("details", [])
                            for detail in details:
                                if detail.get("reason"):
                                    error_reason = detail.get("reason")
                                    break
                            if error_message:
                                error_reason = f"{error_reason}: {error_message}"
                    except Exception:
                        error_reason = (
                            f"HTTP Error {response.status_code}: {response.text[:200]}"
                        )

                    debug_logger.log_error(f"[API FAILED] URL: {url}")
                    debug_logger.log_error(
                        f"[API FAILED] Request Body: {request_body_for_log}"
                    )
                    debug_logger.log_error(f"[API FAILED] Response: {response.text}")
                    raise Exception(error_reason)

                return response.json()

        except Exception as e:
            error_msg = str(e)
            if "HTTP Error" not in error_msg and not any(
                x in error_msg for x in ["PUBLIC_ERROR", "INVALID_ARGUMENT"]
            ):
                debug_logger.log_error(f"[API FAILED] URL: {url}")
                debug_logger.log_error(
                    f"[API FAILED] Request Body: {request_body_for_log}"
                )
                debug_logger.log_error(f"[API FAILED] Exception: {error_msg}")

            if allow_urllib_fallback and self._should_fallback_to_urllib(error_msg):
                debug_logger.log_warning(
                    f"[HTTP FALLBACK] curl_cffi 请求失败，回退 urllib: {method.upper()} {url}"
                )
                try:
                    return await asyncio.to_thread(
                        self._sync_json_request_via_urllib,
                        method.upper(),
                        url,
                        headers,
                        json_data,
                        proxy_url,
                        request_timeout,
                    )
                except Exception as fallback_error:
                    debug_logger.log_error(
                        f"[HTTP FALLBACK] urllib 回退也失败: {fallback_error}"
                    )
                    raise Exception(
                        f"Flow API request failed: curl={error_msg}; urllib={fallback_error}"
                    )

            raise Exception(f"Flow API request failed: {error_msg}")

    async def _make_text_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        raw_body: Optional[Union[str, bytes]] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None,
        timeout: Optional[int] = None,
        respect_fingerprint_proxy: bool = True,
        force_no_proxy: bool = False,
        apply_default_client_headers: bool = True,
        return_error_response: bool = False,
        redact_sensitive_logs: bool = False,
        impersonate: str = "chrome",
        cookie_jar: Optional[CookieJar] = None,
    ) -> str:
        """执行原始文本请求（如 SSE），返回响应文本。"""
        fingerprint = self._request_fingerprint_ctx.get()

        proxy_url = None
        if not force_no_proxy:
            if self.proxy_manager:
                if hasattr(self.proxy_manager, "get_request_proxy_url"):
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                else:
                    proxy_url = await self.proxy_manager.get_proxy_url()

            if (
                respect_fingerprint_proxy
                and isinstance(fingerprint, dict)
                and "proxy_url" in fingerprint
            ):
                proxy_url = fingerprint.get("proxy_url")
                if proxy_url == "":
                    proxy_url = None

        request_timeout = timeout or self.timeout

        if headers is None:
            headers = {}
        else:
            headers = dict(headers)

        if use_st and st_token:
            headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"

        if use_at and at_token:
            headers["authorization"] = f"Bearer {at_token}"

        account_id = None
        if st_token:
            account_id = st_token[:16]
        elif at_token:
            account_id = at_token[:16]

        fingerprint_user_agent = None
        if isinstance(fingerprint, dict):
            fingerprint_user_agent = fingerprint.get("user_agent")

        effective_user_agent = str(
            fingerprint_user_agent or self._generate_user_agent(account_id)
        ).strip()
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("User-Agent", effective_user_agent)
        headers.setdefault(
            "Accept-Language",
            self._get_primary_accept_language(fallback="zh-CN,zh;q=0.9"),
        )

        if isinstance(fingerprint, dict):
            if fingerprint.get("accept_language"):
                headers.setdefault("Accept-Language", fingerprint["accept_language"])
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = self._normalize_sec_ch_ua_header(
                    fingerprint["sec_ch_ua"],
                    user_agent=headers.get("User-Agent"),
                )
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = fingerprint["sec_ch_ua_mobile"]
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = fingerprint["sec_ch_ua_platform"]
            if self._should_attach_runtime_session_cookies(url):
                origin = (
                    str(fingerprint.get("origin") or "").strip()
                    or "https://flow.google.com"
                )
                referer = str(fingerprint.get("referer") or "").strip()
                if not referer:
                    fingerprint_project_id = str(
                        fingerprint.get("project_id") or ""
                    ).strip()
                    if fingerprint_project_id:
                        referer = self._build_flow_project_page_url(
                            fingerprint_project_id
                        )
                if origin:
                    headers.setdefault("Origin", origin)
                if referer:
                    headers.setdefault("Referer", referer)
            if self._should_attach_runtime_session_cookies(url):
                merged_cookie_header = self._merge_cookie_header(
                    headers.get("Cookie"),
                    fingerprint.get("session_cookies"),
                )
                if merged_cookie_header:
                    headers["Cookie"] = merged_cookie_header

        if apply_default_client_headers:
            for key, value in self._default_client_headers.items():
                headers.setdefault(key, value)

        if self._should_attach_runtime_session_cookies(url):
            derived_project_id = self._extract_project_id_from_request_payload(
                json_data
            )
            headers.setdefault("Origin", "https://flow.google.com")
            if derived_project_id:
                headers.setdefault(
                    "Referer", self._build_flow_project_page_url(derived_project_id)
                )

        inferred_fingerprint = self._build_fingerprint_from_user_agent(
            headers.get("User-Agent"),
            accept_language=headers.get("Accept-Language"),
            proxy_url=proxy_url,
        )
        if not headers.get("sec-ch-ua") and inferred_fingerprint.get("sec_ch_ua"):
            headers["sec-ch-ua"] = inferred_fingerprint["sec_ch_ua"]
        if not headers.get("sec-ch-ua-platform") and inferred_fingerprint.get(
            "sec_ch_ua_platform"
        ):
            headers["sec-ch-ua-platform"] = inferred_fingerprint["sec_ch_ua_platform"]
        if not headers.get("sec-ch-ua-mobile") and inferred_fingerprint.get(
            "sec_ch_ua_mobile"
        ):
            headers["sec-ch-ua-mobile"] = inferred_fingerprint["sec_ch_ua_mobile"]

        if cookie_jar is not None:
            # The scoped jar is authoritative, including cookies deleted on retry.
            headers.pop("Cookie", None)
        if isinstance(cookie_jar, FlowCookieJar):
            cookie_jar.add_runtime_cookies(
                (fingerprint or {}).get("session_cookies"), url
            )

        request_body_for_log = (
            "<redacted>"
            if redact_sensitive_logs
            else (raw_body if raw_body is not None else json_data)
        )
        if config.debug_enabled:
            if isinstance(fingerprint, dict):
                proxy_for_log = proxy_url if proxy_url else "direct"
                debug_logger.log_info(
                    f"[FINGERPRINT] 使用打码浏览器指纹提交文本请求: UA={headers.get('User-Agent', '')[:120]}, proxy={proxy_for_log}"
                )
            debug_logger.log_request(
                method=method,
                url=url,
                headers={} if redact_sensitive_logs else headers,
                body=request_body_for_log,
                proxy=proxy_url,
            )

        start_time = time.time()

        try:
            session_kwargs: Dict[str, Any] = {"trust_env": False}
            if cookie_jar is not None:
                session_kwargs.update(
                    cookies=cookie_jar, curl_infos=[CurlInfo.COOKIELIST]
                )
            if isinstance(cookie_jar, FlowCookieJar):
                extra = cookie_jar.extra_cookie_header(url)
                session_kwargs.update(
                    curl_options={CurlOpt.COOKIE: extra}, allow_redirects=not bool(extra)
                )
            async with AsyncSession(**session_kwargs) as session:
                if method.upper() == "GET":
                    response = await session.get(
                        url,
                        headers=headers,
                        proxy=proxy_url,
                        timeout=request_timeout,
                        impersonate=impersonate,
                    )
                else:
                    request_kwargs = {
                        "headers": headers,
                        "proxy": proxy_url,
                        "timeout": request_timeout,
                        "impersonate": impersonate,
                    }
                    if raw_body is not None:
                        request_kwargs["data"] = raw_body
                    else:
                        request_kwargs["json"] = json_data
                    response = await session.post(url, **request_kwargs)

                if cookie_jar is not None:
                    # 兼容自定义/旧版 flow_frontend：缺少该方法时跳过快照回写
                    updater = getattr(self, "_update_flow_cookie_jar", None)
                    if updater is not None:
                        updater(cookie_jar, response)

                duration_ms = (time.time() - start_time) * 1000
                response_text = response.text

                if config.debug_enabled:
                    debug_logger.log_response(
                        status_code=response.status_code,
                        headers={} if redact_sensitive_logs else dict(response.headers),
                        body="<redacted>" if redact_sensitive_logs else response_text,
                        duration_ms=duration_ms,
                    )

                if response.status_code >= 400 and return_error_response:
                    return response_text

                if response.status_code >= 400:
                    error_reason = (
                        f"HTTP Error {response.status_code}: {response_text[:500]}"
                    )
                    try:
                        error_body = response.json()
                        if "error" in error_body:
                            error_info = error_body["error"]
                            error_message = error_info.get("message", "")
                            details = error_info.get("details", [])
                            for detail in details:
                                if detail.get("reason"):
                                    error_reason = detail.get("reason")
                                    break
                            if error_message:
                                error_reason = f"{error_reason}: {error_message}"
                    except Exception:
                        pass

                    debug_logger.log_error(f"[API FAILED] URL: {url}")
                    debug_logger.log_error(
                        f"[API FAILED] Request Body: {request_body_for_log}"
                    )
                    debug_logger.log_error(
                        "[API FAILED] Response: <redacted>"
                        if redact_sensitive_logs
                        else f"[API FAILED] Response: {response_text}"
                    )
                    raise Exception(error_reason)

                return response_text

        except Exception as e:
            error_msg = str(e)
            if "HTTP Error" not in error_msg and not any(
                x in error_msg for x in ["PUBLIC_ERROR", "INVALID_ARGUMENT"]
            ):
                debug_logger.log_error(f"[API FAILED] URL: {url}")
                debug_logger.log_error(
                    f"[API FAILED] Request Body: {request_body_for_log}"
                )
                debug_logger.log_error(f"[API FAILED] Exception: {error_msg}")
            raise Exception(f"Flow API text request failed: {error_msg}")

    def _should_fallback_to_urllib(self, error_message: str) -> bool:
        """判断是否应从 curl_cffi 回退到 urllib。"""
        error_lower = (error_message or "").lower()
        return any(
            keyword in error_lower
            for keyword in [
                "curl: (6)",
                "curl: (7)",
                "curl: (28)",
                "curl: (35)",
                "curl: (52)",
                "curl: (56)",
                "connection timed out",
                "could not connect",
                "failed to connect",
                "ssl connect error",
                "tls connect error",
                "network is unreachable",
            ]
        )

    def _sync_json_request_via_urllib(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, Any]],
        json_data: Optional[Dict[str, Any]],
        proxy_url: Optional[str],
        timeout: int,
    ) -> Dict[str, Any]:
        """使用 urllib 执行 JSON 请求，作为 curl_cffi 的网络回退。"""
        request_headers = dict(headers or {})
        request_headers.setdefault("Accept", "application/json")
        request_headers["Accept-Encoding"] = "identity"

        data = None
        if method.upper() != "GET" and json_data is not None:
            data = json.dumps(json_data, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        handlers = [urllib.request.HTTPSHandler(context=ssl.create_default_context())]
        if proxy_url:
            handlers.append(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )

        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(
            url=url,
            data=data,
            headers=request_headers,
            method=method.upper(),
        )

        try:
            with opener.open(
                request,
                timeout=timeout,
            ) as response:
                payload = response.read()
                status_code = int(response.getcode() or 0)
                content_encoding = str(
                    response.headers.get("Content-Encoding") or ""
                ).lower()
        except urllib.error.HTTPError as exc:
            payload = exc.read() if hasattr(exc, "read") else b""
            status_code = int(getattr(exc, "code", 500) or 500)
            content_encoding = str(
                getattr(exc, "headers", {}).get("Content-Encoding") or ""
            ).lower()
            if content_encoding == "gzip" and payload:
                try:
                    payload = gzip.decompress(payload)
                except Exception:
                    pass
            body_text = payload.decode("utf-8", errors="replace")
            raise Exception(f"HTTP Error {status_code}: {body_text[:200]}") from exc
        except Exception as exc:
            raise Exception(str(exc)) from exc

        if content_encoding == "gzip" and payload:
            try:
                payload = gzip.decompress(payload)
            except Exception:
                pass
        body_text = payload.decode("utf-8", errors="replace")
        if status_code >= 400:
            raise Exception(f"HTTP Error {status_code}: {body_text[:200]}")

        try:
            return json.loads(body_text) if body_text else {}
        except Exception as exc:
            raise Exception(f"Invalid JSON response: {body_text[:200]}") from exc

    def _is_timeout_error(self, error: Exception) -> bool:
        """判断是否为网络超时，便于快速失败重试。"""
        error_lower = str(error).lower()
        return any(
            keyword in error_lower
            for keyword in [
            "timed out",
            "timeout",
            "curl: (28)",
            "connection timed out",
            "operation timed out",
            ]
        )

    def _is_proxy_connection_error(self, error: Exception) -> bool:
        """识别本地/上游代理不可用导致的连接失败。"""
        error_lower = str(error).lower()
        return any(
            keyword in error_lower
            for keyword in [
            "failed to connect to 127.0.0.1 port",
            "failed to connect to localhost port",
            "proxyerror",
            "proxy error",
            "failed to connect to proxy",
            "couldn't connect to server",
            "curl: (7)",
            ]
        )

    def _is_retryable_network_error(self, error_str: str) -> bool:
        """识别可重试的 TLS/连接类网络错误。"""
        error_lower = (error_str or "").lower()
        return any(
            keyword in error_lower
            for keyword in [
            "curl: (35)",
            "curl: (52)",
            "curl: (56)",
            "ssl_error_syscall",
            "tls connect error",
            "ssl connect error",
            "connection reset",
            "connection aborted",
            "connection was reset",
            "connection timed out",
            "curl: (28)",
            "timed out",
            "timeout",
            "unexpected eof",
            "empty reply from server",
            "recv failure",
            "send failure",
            "connection refused",
            "network is unreachable",
            "remote host closed connection",
            ]
        )

    def _get_control_plane_timeout(self) -> int:
        """控制轻量控制面请求的超时，避免认证/项目接口长时间挂起。"""
        return max(5, min(int(self.timeout or 0) or 120, 10))

    def _get_video_submit_timeout(self) -> int:
        """视频提交接口应快速返回 operation，避免单次网络挂死拖满整条链路。"""
        return max(30, min(int(self.timeout or 0) or 120, 75))

    def _get_video_poll_timeout(self) -> int:
        """视频状态查询是轻量轮询，请求超时不应超过下一轮轮询太久。"""
        return max(10, min(int(self.timeout or 0) or 120, 45))

    def _resolve_generation_retry_budget(
        self, base_max_retries: int, error: Optional[Union[Exception, str]] = None
    ) -> int:
        """计算当前生成链路允许的总重试次数。"""
        effective_max_retries = max(1, int(base_max_retries or 1))
        if error is None:
            return effective_max_retries

        error_str = str(error)
        error_lower = error_str.lower()
        if (
            "recaptcha evaluation failed" in error_lower
            or "recaptcha 验证失败" in error_str
        ):
            return max(
                effective_max_retries,
                int(config.browser_captcha_generation_retries or 6),
            )
        return effective_max_retries

    def _build_realistic_video_submit_headers(self) -> Dict[str, str]:
        """构造当前上游真实抓包风格的视频提交头。"""
        return self._build_current_flow_media_headers(
            content_type="text/plain;charset=UTF-8"
        )

    def _resolve_runtime_impersonate(self, fallback: str = "chrome") -> str:
        resolved = self._resolve_impersonate_from_fingerprint(fallback=fallback)
        return resolved or fallback

    def _resolve_impersonate_from_fingerprint(self, fallback: str = "chrome") -> str:
        """根据当前请求链路绑定的浏览器指纹，选择最接近的 curl_cffi impersonate。"""
        fingerprint = self.get_request_fingerprint()
        if not isinstance(fingerprint, dict):
            return fallback

        ua = str(fingerprint.get("user_agent") or "").strip()
        ua_lower = ua.lower()
        if not ua_lower:
            return fallback

        if "android" in ua_lower:
            return "chrome_android"
        if "edg/" in ua_lower or " edge/" in ua_lower:
            return "edge"
        if (
            "safari/" in ua_lower
            and "chrome/" not in ua_lower
            and "chromium/" not in ua_lower
        ):
            if "iphone" in ua_lower or "ipad" in ua_lower or "ios" in ua_lower:
                return "safari_ios"
            return "safari"

        if "chrome/" not in ua_lower and "chromium/" not in ua_lower:
            return fallback

        return "chrome"

    async def _acquire_image_launch_gate(
        self,
        token_id: Optional[int],
        token_image_concurrency: Optional[int],
    ) -> tuple[bool, int, int]:
        """图片请求不再做本地发车排队，直接进入取 token 并提交上游。"""
        return True, 0, 0

    async def _release_image_launch_gate(self, token_id: Optional[int]):
        """保留接口形状，当前无需释放任何本地发车状态。"""
        return

    async def _acquire_video_launch_gate(
        self,
        token_id: Optional[int],
        token_video_concurrency: Optional[int],
    ) -> tuple[bool, int, int]:
        """视频请求不再做本地发车排队，直接进入取 token 并提交上游。"""
        return True, 0, 0

    async def _release_video_launch_gate(self, token_id: Optional[int]):
        """保留接口形状，当前无需释放任何本地发车状态。"""
        return

    # ========== 图片上传 (使用AT) ==========

    def _detect_image_mime_type(self, image_bytes: bytes) -> str:
        """通过文件头 magic bytes 检测图片 MIME 类型

        Args:
            image_bytes: 图片字节数据

        Returns:
            MIME 类型字符串，默认 image/jpeg
        """
        if len(image_bytes) < 12:
            return "image/jpeg"

        # WebP: RIFF....WEBP
        if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            return "image/webp"
        # PNG: 89 50 4E 47
        if image_bytes[:4] == b"\x89PNG":
            return "image/png"
        # JPEG: FF D8 FF
        if image_bytes[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        # GIF: GIF87a 或 GIF89a
        if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        # BMP: BM
        if image_bytes[:2] == b"BM":
            return "image/bmp"
        # JPEG 2000: 00 00 00 0C 6A 50
        if image_bytes[:6] == b"\x00\x00\x00\x0cjP":
            return "image/jp2"

        return "image/jpeg"

    # ========== 辅助方法 ==========

    async def _handle_retryable_generation_error(
        self,
        error: Exception,
        retry_attempt: int,
        max_retries: int,
        browser_id: Optional[Union[int, str]],
        project_id: str,
        log_prefix: str,
        defer_browser_error_notification: bool = False,
    ) -> bool:
        """统一处理生成链路的重试判定与打码自愈通知。"""
        error_str = str(error)
        retry_reason = self._get_retry_reason(error_str)
        retry_delay = self._get_retry_delay_seconds(error_str, retry_attempt)

        effective_max_retries = self._resolve_generation_retry_budget(
            max_retries, error_str
        )
        is_terminal_attempt = retry_attempt >= effective_max_retries - 1
        should_notify_browser = (
            not defer_browser_error_notification
            or not retry_reason
            or is_terminal_attempt
            or (config.captcha_method == "browser" and "recaptcha" in error_str.lower())
        )

        if should_notify_browser:
            notify_reason = retry_reason or error_str[:120] or type(error).__name__
            await self._notify_browser_captcha_error(
                browser_id=getattr(error, "browser_ref", None) or browser_id,
                project_id=project_id,
                error_reason=notify_reason,
                error_message=error_str,
            )

        if not retry_reason or getattr(error, "submission_uncertain", False):
            return False

        if is_terminal_attempt:
            debug_logger.log_warning(
                f"{log_prefix}遇到{retry_reason}，已达到最大重试次数({effective_max_retries})，本次请求失败并执行关闭回收。"
            )
            return False

        debug_logger.log_warning(
            f"{log_prefix}遇到{retry_reason}，将在 {retry_delay} 秒后重新获取验证码重试 ({retry_attempt + 2}/{effective_max_retries})..."
        )
        await asyncio.sleep(retry_delay)
        return True

    async def _handle_missing_recaptcha_token(
        self,
        retry_attempt: int,
        max_retries: int,
        browser_id: Optional[Union[int, str]],
        project_id: str,
        log_prefix: str,
    ) -> bool:
        token_error = Exception("Failed to obtain reCAPTCHA token")
        return await self._handle_retryable_generation_error(
            error=token_error,
            retry_attempt=retry_attempt,
            max_retries=max_retries,
            browser_id=browser_id,
            project_id=project_id,
            log_prefix=log_prefix,
        )

    def _resolve_third_party_captcha_method(self) -> Optional[str]:
        """返回第一个已配置密钥的第三方打码方式（yescaptcha 优先）。

        SPrCad 等 batchexecute RPC 只接受第三方打码 token；
        browser/personal harvest 出的 token 会被 UNUSUAL_ACTIVITY 拒绝。
        """
        for method, attr in (
            ("yescaptcha", "yescaptcha_api_key"),
            ("capmonster", "capmonster_api_key"),
            ("ezcaptcha", "ezcaptcha_api_key"),
            ("capsolver", "capsolver_api_key"),
            ("captcharun", "captcharun_api_key"),
            ("twocaptcha", "twocaptcha_api_key"),
        ):
            if str(getattr(config, attr, "") or "").strip():
                return method
        return None

    @staticmethod
    def _extract_browser_captcha_session_id(
        browser_ref: Optional[Union[int, str]],
    ) -> Optional[str]:
        """从 browser 请求句柄提取本次 harvest 对应的 Flow session id。"""
        if not isinstance(browser_ref, str):
            return None
        raw = browser_ref.strip()
        if raw.startswith("personal:"):
            return raw.partition("personal:")[2] or None
        browser_id, separator, request_ref = raw.partition(":")
        if not separator or not browser_id.isdigit() or not request_ref:
            return None
        if request_ref.startswith("lease-"):
            return request_ref.partition("/")[2] or None
        return request_ref

    def _get_retry_reason(self, error_str: str) -> Optional[str]:
        """判断是否需要重试，返回日志提示内容"""
        error_lower = error_str.lower()
        if "browser captcha queue wait timeout" in error_lower:
            return None
        if "error_no_slot_available_block" in error_lower:
            return "打码服务资源阻塞"
        if "error_no_slot_available" in error_lower:
            return "打码服务资源不足"
        if "model_access_denied" in error_lower:
            # 账号无对应模型权限，重试不会改变结果
            return None
        if "public_error_user_quota_reached" in error_lower:
            # 账号额度不足，同号重试只会白白消耗打码；交给上层换号
            return None
        if "403" in error_lower:
            return "403错误"
        if "429" in error_lower or "too many requests" in error_lower:
            return "429限流"
        if self._is_retryable_network_error(error_str):
            return "网络/TLS错误"
        if "recaptcha evaluation failed" in error_lower:
            return "reCAPTCHA 验证失败"
        if "recaptcha" in error_lower:
            return "reCAPTCHA 错误"
        if any(
            keyword in error_lower
            for keyword in [
            "http error 500",
            "public_error",
            "internal error",
            "reason=internal",
            "reason: internal",
                '"reason":"internal"',
            "server error",
            "upstream error",
            ]
        ):
            return "500/内部错误"
        return None

    def _get_retry_delay_seconds(self, error_str: str, retry_attempt: int) -> int:
        error_lower = str(error_str or "").lower()
        if "error_no_slot_available_block" in error_lower:
            return 20
        if "error_no_slot_available" in error_lower:
            index = max(
                0, min(retry_attempt, len(self.YESCAPTCHA_SLOT_BACKOFF_SECONDS) - 1)
            )
            return self.YESCAPTCHA_SLOT_BACKOFF_SECONDS[index]
        if "recaptcha evaluation failed" in error_lower:
            return 2
        if "recaptcha" in error_lower:
            return 2
        if "public_error_unusual_activity" in error_lower:
            # 风控拒绝后立刻重试几乎必然再次被拒，留出打码服务自愈时间
            return 3
        return 1

    def _resolve_recaptcha_runtime_settings(
        self,
        method: str,
        action: str,
    ) -> Dict[str, Any]:
        normalized_action = (
            str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        )
        website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"

        if method == "yescaptcha":
            page_action = normalized_action
            task_type = config.yescaptcha_task_type
            min_score = get_yescaptcha_min_score(task_type)
        elif method == "capmonster":
            page_action = normalized_action
            task_type = "RecaptchaV3TaskProxyless"
            min_score = None
        elif method == "ezcaptcha":
            page_action = normalized_action
            task_type = "ReCaptchaV3TaskProxylessS9"
            min_score = None
        elif method == "capsolver":
            page_action = normalized_action
            task_type = "ReCaptchaV3EnterpriseTaskProxyLess"
            min_score = None
        elif method == "captcharun":
            page_action = normalized_action
            task_type = "ReCaptchaV3"
            min_score = None
        elif method == "twocaptcha":
            page_action = normalized_action
            task_type = "RecaptchaV3TaskProxyless"
            min_score = 0.9
        else:
            page_action = normalized_action
            task_type = None
            min_score = None

        return {
            "website_key": website_key,
            "page_action": page_action,
            "task_type": task_type,
            "min_score": min_score,
        }

    def _merge_request_fingerprint(self, patch: Optional[Dict[str, Any]]):
        if not isinstance(patch, dict) or not patch:
            return
        current = self.get_request_fingerprint() or {}
        merged = dict(current)
        for key, value in patch.items():
            if value is None:
                continue
            if isinstance(value, str):
                normalized = value.strip()
                if not normalized:
                    continue
                merged[key] = normalized
            else:
                merged[key] = value
        self._set_request_fingerprint(merged if merged else None)

    async def _notify_browser_captcha_error(
        self,
        browser_id: Optional[Union[int, str]] = None,
        project_id: Optional[str] = None,
        error_reason: Optional[str] = None,
        error_message: Optional[str] = None,
    ):
        """通知浏览器打码服务执行失败自愈。
        
        Args:
            browser_id: browser 模式使用的浏览器 ID
            project_id: personal 模式使用的 project_id
            error_reason: 已归类的错误原因
            error_message: 原始错误文本
        """
        context = self._get_captcha_request_context()
        if context.get("browser_ref") != browser_id or context.get("project_id") != project_id:
            context = {}
        method = context.get("method") or config.captcha_method
        if method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_error(
                    browser_id,
                    error_reason=error_message or error_reason or "upstream_error",
                )
            except Exception:
                pass
        elif method == "extension":
            try:
                from .browser_captcha_extension import ExtensionCaptchaService

                service = await ExtensionCaptchaService.get_instance()
                await service.report_flow_error(
                    project_id=project_id,
                    error_reason=error_reason or "",
                    error_message=error_message or "",
                )
            except Exception:
                pass
        elif method == "personal" and project_id:
            try:
                from .browser_captcha_personal import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_flow_error(
                    project_id=project_id,
                    error_reason=error_reason or "",
                    error_message=error_message or "",
                    token_id=context.get("token_id"),
                    slot_id=context.get("slot_id"),
                    issued_at=context.get("issued_at"),
                )
            except Exception:
                pass
        elif method == "remote_browser" and browser_id:
            try:
                session_id = quote(str(browser_id), safe="")
                await self._call_remote_browser_service(
                    method="POST",
                    path=f"/api/v1/sessions/{session_id}/error",
                    json_data={
                        "error_reason": error_reason
                        or error_message
                        or "upstream_error"
                    },
                    timeout_override=2,
                )
            except Exception as e:
                debug_logger.log_warning(
                    f"[reCAPTCHA RemoteBrowser] 上报 error 失败: {e}"
                )

    async def _notify_browser_captcha_request_finished(
        self, browser_id: Optional[Union[int, str]] = None, *, success: Optional[bool] = None,
    ):
        """通知有头浏览器：上游图片/视频请求已结束，可关闭对应打码浏览器。"""
        context = self._get_captcha_request_context()
        method = context.get("method") if context.get("browser_ref") == browser_id else None
        method = method or config.captcha_method
        if method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                if success is None:
                    await service.report_request_finished(browser_id)
                else:
                    await service.report_request_finished(browser_id, success=success)
            except Exception:
                pass
        elif method == "remote_browser" and browser_id:
            try:
                session_id = quote(str(browser_id), safe="")
                await self._call_remote_browser_service(
                    method="POST",
                    path=f"/api/v1/sessions/{session_id}/finish",
                    json_data={"status": "success"},
                    timeout_override=2,
                )
            except Exception as e:
                debug_logger.log_warning(
                    f"[reCAPTCHA RemoteBrowser] 上报 finish 失败: {e}"
                )
        context = self._get_captcha_request_context()
        if context.get("browser_ref") == browser_id:
            self._set_captcha_request_context(None)

    def _generate_session_id(self) -> str:
        """生成sessionId: ;timestamp"""
        return f";{int(time.time() * 1000)}"

    def _generate_scene_id(self) -> str:
        """生成sceneId: UUID"""
        return str(uuid.uuid4())

    def _get_remote_browser_service_config(self) -> tuple[str, str, int]:
        base_url = (config.remote_browser_base_url or "").strip().rstrip("/")
        api_key = (config.remote_browser_api_key or "").strip()
        timeout = max(5, int(config.remote_browser_timeout or 60))

        if not base_url:
            raise RuntimeError("remote_browser 服务地址未配置")
        if not api_key:
            raise RuntimeError("remote_browser API Key 未配置")

        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise RuntimeError("remote_browser 服务地址格式错误")

        return base_url, api_key, timeout

    @staticmethod
    def _build_remote_browser_http_timeout(read_timeout: float) -> Any:
        read_value = max(3.0, float(read_timeout))
        write_value = min(10.0, max(3.0, read_value))
        if httpx is None:
            return read_value
        return httpx.Timeout(
            connect=2.5,
            read=read_value,
            write=write_value,
            pool=2.5,
        )

    @staticmethod
    def _parse_json_response_text(text: str) -> Optional[Any]:
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    async def _stdlib_json_http_request(
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout: int,
    ) -> tuple[int, Optional[Any], str]:
        req_headers = dict(headers or {})
        req_headers.setdefault("Accept", "application/json")
        request_method = (method or "GET").upper()
        request_data: Optional[bytes] = None

        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
            if request_method != "GET":
                request_data = json.dumps(payload).encode("utf-8")

        def do_request() -> tuple[int, str]:
            request = urllib.request.Request(
                url=url,
                data=request_data,
                headers=req_headers,
                method=request_method,
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(request, timeout=max(1.0, float(timeout))) as response:
                    status_code = int(
                        getattr(response, "status", 0) or response.getcode() or 0
                    )
                    body = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                    return status_code, body.decode(charset, errors="replace")
            except urllib.error.HTTPError as exc:
                body = exc.read()
                charset = exc.headers.get_content_charset() if exc.headers else None
                return int(getattr(exc, "code", 0) or 0), body.decode(
                    charset or "utf-8", errors="replace"
                )

        try:
            status_code, text = await asyncio.to_thread(do_request)
        except Exception as e:
            raise RuntimeError(f"remote_browser 请求失败: {e}") from e

        return status_code, FlowClient._parse_json_response_text(text), text

    @staticmethod
    async def _sync_json_http_request(
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout: int,
    ) -> tuple[int, Optional[Any], str]:
        req_headers = dict(headers or {})
        req_headers.setdefault("Accept", "application/json")
        request_method = (method or "GET").upper()
        request_kwargs: Dict[str, Any] = {
            "headers": req_headers,
            "timeout": FlowClient._build_remote_browser_http_timeout(timeout),
        }

        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
            if request_method != "GET":
                request_kwargs["json"] = payload

        if httpx is None:
            return await FlowClient._stdlib_json_http_request(
                method=method,
                url=url,
                headers=req_headers,
                payload=payload,
                timeout=timeout,
            )

        try:
            # remote_browser 控制面只需要稳定传输 JSON，不需要浏览器指纹伪装。
            # 使用 httpx 可以避免 curl_cffi 在当前环境下 POST body 被吞掉。
            async with httpx.AsyncClient(
                follow_redirects=False, trust_env=False
            ) as session:
                response = await session.request(
                    method=request_method,
                    url=url,
                    **request_kwargs,
                )
        except Exception as e:
            raise RuntimeError(f"remote_browser 请求失败: {e}") from e

        status_code = int(getattr(response, "status_code", 0) or 0)
        text = response.text or ""
        parsed = FlowClient._parse_json_response_text(text)

        return status_code, parsed, text

    async def _call_remote_browser_service(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        timeout_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        base_url, api_key, timeout = self._get_remote_browser_service_config()
        url = f"{base_url}{path}"
        effective_timeout = max(5, int(timeout_override or timeout))

        status_code, payload, response_text = await self._sync_json_http_request(
            method=method,
            url=url,
            headers={"Authorization": f"Bearer {api_key}"},
            payload=json_data,
            timeout=effective_timeout,
        )

        if status_code >= 400:
            detail = ""
            if isinstance(payload, dict):
                detail = payload.get("detail") or payload.get("message") or str(payload)
            if not detail:
                detail = (response_text or "").strip() or f"HTTP {status_code}"
            raise RuntimeError(f"remote_browser 请求失败: {detail}")

        if not isinstance(payload, dict):
            raise RuntimeError("remote_browser 返回格式错误")

        return payload

    async def prefill_remote_browser_pool(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        *,
        cooldown_seconds: float = 8.0,
    ) -> bool:
        """让本地 remote_browser 服务提前开始补池，尽量把取 token 等待搬到前面。"""
        if config.captcha_method != "remote_browser":
            return False

        normalized_project = str(project_id or "").strip()
        normalized_action = (
            str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        )
        if not normalized_project:
            return False

        cache_key = f"{normalized_project}|{normalized_action}|{int(token_id or 0)}"
        now_value = time.monotonic()
        last_sent = float(
            self._remote_browser_prefill_last_sent.get(cache_key, 0.0) or 0.0
        )
        if (now_value - last_sent) < max(0.5, float(cooldown_seconds)):
            return False

        try:
            await self._call_remote_browser_service(
                method="POST",
                path="/api/v1/prefill",
                json_data={
                    "project_id": normalized_project,
                    "action": normalized_action,
                    "token_id": token_id,
                },
                timeout_override=3,
            )
            self._remote_browser_prefill_last_sent[cache_key] = now_value
            return True
        except Exception as e:
            debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] prefill 失败: {e}")
            return False

    async def prefill_remote_browser_for_tokens(
        self, tokens: List[Any], action: str = "IMAGE_GENERATION"
    ) -> int:
        if config.captcha_method != "remote_browser":
            return 0

        unique_projects: List[str] = []
        seen_projects = set()
        for token in tokens or []:
            project_id = str(getattr(token, "current_project_id", "") or "").strip()
            if not project_id or project_id in seen_projects:
                continue
            seen_projects.add(project_id)
            unique_projects.append(project_id)

        warmed = 0
        for project_id in unique_projects:
            if await self.prefill_remote_browser_pool(project_id, action=action):
                warmed += 1
        return warmed

    def _resolve_remote_browser_solve_timeout(self, action: str) -> int:
        base_timeout = max(5, int(config.remote_browser_timeout or 60))
        action_name = str(action or "").strip().upper()

        # 这里只是拿 reCAPTCHA token，不应该跟整条生成链路共用数百秒级超时。
        target_timeout = 45 if action_name == "VIDEO_GENERATION" else 35
        return max(12, min(base_timeout, target_timeout))

    async def _get_recaptcha_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        website_url: Optional[str] = None,
        method_override: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[Union[int, str]]]:
        """获取reCAPTCHA token - 支持多种打码方式
        
        Args:
            project_id: 项目ID
            action: reCAPTCHA action类型
                - IMAGE_GENERATION: 图片生成和2K/4K图片放大 (默认)
                - VIDEO_GENERATION: 视频生成和视频放大
            token_id: 当前业务 token id（browser 模式下用于读取 token 级打码代理）
            method_override: 强制指定打码方式（如 "yescaptcha"）。
                用于 harvest token 不被目标 RPC 接受的场景（如 SPrCad 放大）。
        
        Returns:
            (token, browser_id) 元组。
            - browser 模式: browser_id 为本地浏览器 ID
            - remote_browser 模式: browser_id 为远程 session_id
            - 其他模式: browser_id 为 None
        """
        captcha_method = method_override or config.captcha_method
        previous = self._get_captcha_request_context()
        if previous.get("method") == "browser" and previous.get("owner") is asyncio.current_task():
            await self._notify_browser_captcha_request_finished(previous.get("browser_ref"))
        self._set_captcha_request_context({
            "method": captcha_method, "token_id": token_id, "project_id": project_id,
            "owner": asyncio.current_task(),
        })
        debug_logger.log_info(
            f"[reCAPTCHA] 开始获取 token: method={captcha_method}, project_id={project_id}, action={action}"
        )

        if captcha_method == "extension":
            try:
                from .browser_captcha_extension import ExtensionCaptchaService

                service = await ExtensionCaptchaService.get_instance(self.db)
                extension_timeout = 45 if action == "VIDEO_GENERATION" else 25
                token = await service.get_token(
                    project_id, action, timeout=extension_timeout, token_id=token_id
                )
                self._set_request_fingerprint(None)
                return token, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Extension] 错误: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None

        # 内置浏览器打码 (nodriver)
        if captcha_method == "personal":
            debug_logger.log_info(f"[reCAPTCHA] 使用 personal 模式")
            try:
                from .browser_captcha_personal import BrowserCaptchaService

                debug_logger.log_info(f"[reCAPTCHA] 导入 BrowserCaptchaService 成功")
                service = await BrowserCaptchaService.get_instance(self.db)
                debug_logger.log_info(
                    f"[reCAPTCHA] 获取服务实例成功，准备调用 get_token"
                )
                solve_bundle = None
                get_token_bundle = getattr(service, "get_token_bundle", None)
                if callable(get_token_bundle):
                    solve_bundle = await get_token_bundle(
                        project_id,
                        action,
                        token_id=token_id,
                    )
                    token = str((solve_bundle or {}).get("token") or "").strip() or None
                else:
                    get_token_with_metadata = getattr(
                        service, "get_token_with_metadata", None
                    )
                    if callable(get_token_with_metadata):
                        (
                            token,
                            _slot_id,
                            _cookie_source_token_id,
                        ) = await get_token_with_metadata(
                            project_id,
                            action,
                            token_id=token_id,
                        )
                    else:
                        token = await service.get_token(
                            project_id, action, token_id=token_id
                        )
                    solve_bundle = (
                        {
                        "token": token,
                            "fingerprint": service.get_last_fingerprint()
                            if token
                            else None,
                        }
                        if token
                        else None
                    )
                debug_logger.log_info(f"[reCAPTCHA] 取码完成: success={bool(token)}")
                fingerprint = (
                    solve_bundle.get("fingerprint")
                    if isinstance(solve_bundle, dict)
                    and isinstance(solve_bundle.get("fingerprint"), dict)
                    else None
                )
                harvest_session_id = (
                    str((solve_bundle or {}).get("session_id") or "").strip() or None
                )
                if isinstance(solve_bundle, dict) and token:
                    session_cookies = solve_bundle.get("session_cookies")
                    proxy_url = str(solve_bundle.get("proxy_url") or "").strip()
                    next_fingerprint = dict(fingerprint or {})
                    if isinstance(session_cookies, dict) and session_cookies:
                        next_fingerprint["session_cookies"] = dict(session_cookies)
                    if (
                        proxy_url
                        and not str(next_fingerprint.get("proxy_url") or "").strip()
                    ):
                        next_fingerprint["proxy_url"] = proxy_url
                    next_fingerprint["project_id"] = project_id
                    next_fingerprint.setdefault("origin", "https://flow.google.com")
                    next_fingerprint.setdefault(
                        "referer", self._build_flow_project_page_url(project_id)
                    )
                    fingerprint = next_fingerprint or None
                if token:
                    effective_ua = str(
                        (fingerprint or {}).get("user_agent") or ""
                    ).strip()
                    effective_lang = str(
                        (fingerprint or {}).get("accept_language") or ""
                    ).strip()
                    if not effective_ua:
                        debug_logger.log_warning(
                            "[reCAPTCHA Personal] 已拿到 token，但未提取到浏览器指纹 UA；"
                            "为避免协议提交与打码环境失配，丢弃本次 token 并触发重试"
                        )
                        self._set_request_fingerprint(None)
                        return None, None
                    debug_logger.log_info(
                        "[reCAPTCHA Personal] 使用浏览器指纹: "
                        f"UA={effective_ua[:120]}, Accept-Language={effective_lang or '<empty>'}"
                    )
                self._set_request_fingerprint(fingerprint if token else None)
                browser_ref = (
                    f"personal:{harvest_session_id}"
                    if token and harvest_session_id
                    else None
                )
                if token:
                    self._set_captcha_request_context({
                        "method": "personal", "browser_ref": browser_ref,
                        "token_id": token_id, "project_id": project_id,
                        "slot_id": (solve_bundle or {}).get("slot_id"),
                        "issued_at": (solve_bundle or {}).get("issued_at"),
                        "owner": asyncio.current_task(),
                    })
                return token, browser_ref
            except RuntimeError as e:
                # 捕获 Docker 环境或依赖缺失的明确错误
                error_msg = str(e)
                debug_logger.log_error(f"[reCAPTCHA Personal] {error_msg}")
                print(f"[reCAPTCHA] ❌ 内置浏览器打码失败: {error_msg}")
                self._set_request_fingerprint(None)
                return None, None
            except ImportError as e:
                debug_logger.log_error(f"[reCAPTCHA Personal] 导入失败: {str(e)}")
                print(f"[reCAPTCHA] ❌ nodriver 未安装，请运行: pip install nodriver")
                self._set_request_fingerprint(None)
                return None, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Personal] 错误: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        # 有头浏览器打码 (playwright)
        elif captcha_method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(self.db)
                token, browser_id, fingerprint = await service.get_token(
                    project_id, action, token_id=token_id
                )
                if token and action == "CHAT_GENERATION" and not self._extract_browser_captcha_session_id(
                    browser_id
                ):
                    debug_logger.log_warning(
                        "[reCAPTCHA Browser] harvest 未返回会话 id，生成请求可能被拒"
                    )
                self._set_request_fingerprint(fingerprint if token else None)
                if token:
                    self._set_captcha_request_context({
                        "method": "browser", "browser_ref": browser_id,
                        "token_id": token_id, "project_id": project_id,
                        "owner": asyncio.current_task(),
                    })
                return token, browser_id
            except RuntimeError as e:
                from .browser_captcha import BrowserCaptchaQueueTimeout
                if isinstance(e, BrowserCaptchaQueueTimeout):
                    raise
                # 捕获 Docker 环境或依赖缺失的明确错误
                error_msg = str(e)
                debug_logger.log_error(f"[reCAPTCHA Browser] {error_msg}")
                print(f"[reCAPTCHA] ❌ 有头浏览器打码失败: {error_msg}")
                self._set_request_fingerprint(None)
                return None, None
            except ImportError as e:
                debug_logger.log_error(f"[reCAPTCHA Browser] 导入失败: {str(e)}")
                print(
                    f"[reCAPTCHA] ❌ playwright 未安装，请运行: pip install playwright && python -m playwright install chromium"
                )
                self._set_request_fingerprint(None)
                return None, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Browser] 错误: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        elif captcha_method == "remote_browser":
            try:
                solve_timeout = self._resolve_remote_browser_solve_timeout(action)
                payload = await self._call_remote_browser_service(
                    method="POST",
                    path="/api/v1/solve",
                    json_data={
                        "project_id": project_id,
                        "action": action,
                        "token_id": token_id,
                    },
                    timeout_override=solve_timeout,
                )
                token = payload.get("token")
                session_id = payload.get("session_id")
                fingerprint = (
                    payload.get("fingerprint")
                    if isinstance(payload.get("fingerprint"), dict)
                    else None
                )
                self._set_request_fingerprint(fingerprint if token else None)
                if not token or not session_id:
                    raise RuntimeError(
                        f"remote_browser 返回缺少 token/session_id: {payload}"
                    )
                return token, str(session_id)
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA RemoteBrowser] 错误: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        # API打码服务
        elif captcha_method in [
            "yescaptcha",
            "captcharun",
            "twocaptcha",
            "capmonster",
            "ezcaptcha",
            "capsolver",
        ]:
            existing_fingerprint = self.get_request_fingerprint() or {}
            proxy_url = str(existing_fingerprint.get("proxy_url") or "").strip()
            if not proxy_url and self.proxy_manager:
                try:
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                except Exception as e:
                    debug_logger.log_warning(
                        f"[reCAPTCHA] Failed to get proxy for API captcha: {e}"
                    )

            api_captcha_ua = str(
                existing_fingerprint.get("user_agent") or ""
            ).strip() or self._generate_user_agent(
                str(token_id or project_id or captcha_method)
            )
            next_fingerprint = dict(existing_fingerprint)
            next_fingerprint.update(
                self._build_fingerprint_from_user_agent(
                    api_captcha_ua,
                    accept_language=self._get_primary_accept_language(),
                    proxy_url=proxy_url,
                )
            )
            self._set_request_fingerprint(next_fingerprint)
            captcha_website_url = str(
                website_url or ""
            ).strip() or self._build_flow_project_page_url(project_id)
            parsed_captcha_url = urlparse(captcha_website_url)
            captcha_origin = (
                f"{parsed_captcha_url.scheme}://{parsed_captcha_url.netloc}"
                if parsed_captcha_url.scheme and parsed_captcha_url.netloc
                else "https://flow.google.com"
            )
            self._merge_request_fingerprint(
                {
                    "project_id": project_id,
                    "origin": captcha_origin,
                    "referer": captcha_website_url,
                }
            )

            api_result = await self._get_api_captcha_token(
                captcha_method,
                project_id,
                action,
                proxy_url=proxy_url,
                user_agent=api_captcha_ua,
                website_url=captcha_website_url,
                token_id=token_id,
            )
            if api_result is None:
                return None, None
            token, captcha_user_agent = api_result
            # 把打码服务返回的 userAgent 合并到 fingerprint, 让 Flow API 提交请求沿用同一 UA。
            # 否则 Google reCAPTCHA V3 评估会因 UA 不一致判定 UNUSUAL_ACTIVITY 并返回
            # "reCAPTCHA evaluation failed"。其他 Client Hint (sec-ch-ua-platform 等)
            # 由 _make_request 根据 UA 自动推断 (Windows → "Windows", etc.)。
            if captcha_user_agent:
                existing_fp = self._request_fingerprint_ctx.get()
                merged_fp = dict(existing_fp) if isinstance(existing_fp, dict) else {}
                merged_fp["user_agent"] = captcha_user_agent
                self._set_request_fingerprint(merged_fp)
                debug_logger.log_info(
                    f"[reCAPTCHA {captcha_method}] 已将打码 UA 注入请求指纹: "
                    f"{captcha_user_agent[:80]}"
                )
            return token, None
        else:
            debug_logger.log_info(f"[reCAPTCHA] 未知的打码方式: {captcha_method}")
            self._set_request_fingerprint(None)
            return None, None

    async def _get_api_captcha_token(
        self,
        method: str,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        proxy_url: Optional[str] = None,
        user_agent: Optional[str] = None,
        website_url: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> Optional[tuple[str, str]]:
        """通用API打码服务

        Args:
            method: 打码服务类型
            project_id: 项目ID
            action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)
            proxy_url: 预解析后的代理地址，确保打码与后续提交走同一出口
            user_agent: 显式传给打码服务的 UA，避免 solve / submit 环境不一致

        Returns:
            (gRecaptchaResponse, userAgent) 元组, 或 None (失败时)。
            返回 userAgent 是为了让后续 Flow API 请求沿用打码时的 UA,
            避免 reCAPTCHA V3 评估因 UA 不一致判定 UNUSUAL_ACTIVITY。
        """
        # 获取配置
        if method == "yescaptcha":
            client_key = config.yescaptcha_api_key
            base_url = config.yescaptcha_base_url
        elif method == "capmonster":
            client_key = config.capmonster_api_key
            base_url = config.capmonster_base_url
        elif method == "ezcaptcha":
            client_key = config.ezcaptcha_api_key
            base_url = config.ezcaptcha_base_url
        elif method == "capsolver":
            client_key = config.capsolver_api_key
            base_url = config.capsolver_base_url
        elif method == "captcharun":
            client_key = config.captcharun_api_key
            base_url = config.captcharun_base_url
        elif method == "twocaptcha":
            client_key = config.twocaptcha_api_key
            base_url = config.twocaptcha_base_url
        else:
            debug_logger.log_error(f"[reCAPTCHA] Unknown API method: {method}")
            return None

        if not client_key:
            debug_logger.log_info(
                f"[reCAPTCHA] {method} API key not configured, skipping"
            )
            return None

        runtime_settings = self._resolve_recaptcha_runtime_settings(method, action)
        task_type = runtime_settings["task_type"]
        min_score = runtime_settings["min_score"]
        website_key = runtime_settings["website_key"]
        website_url = str(
            website_url or ""
        ).strip() or self._build_flow_project_page_url(project_id)
        page_action = runtime_settings["page_action"]

        try:
            # 获取代理配置，让打码API请求也走代理
            # 注意：curl_cffi 对 SOCKS5 使用 proxy 参数，HTTP 代理使用 proxies 参数
            proxy = None
            proxies = None
            resolved_proxy_url = str(proxy_url or "").strip()
            if not resolved_proxy_url and self.proxy_manager:
                try:
                    resolved_proxy_url = str(
                        await self.proxy_manager.get_request_proxy_url() or ""
                    ).strip()
                    if resolved_proxy_url:
                        if resolved_proxy_url.startswith("socks5://"):
                            # curl_cffi 对 SOCKS5 使用 proxy 参数
                            proxy = resolved_proxy_url
                        else:
                            # HTTP/HTTPS 代理使用 proxies 字典
                            proxies = {
                                "http": resolved_proxy_url,
                                "https": resolved_proxy_url,
                            }
                except Exception as e:
                    debug_logger.log_warning(
                        f"[reCAPTCHA {method}] Failed to get proxy: {e}"
                    )
            elif resolved_proxy_url:
                if resolved_proxy_url.startswith("socks5://"):
                    proxy = resolved_proxy_url
                else:
                    proxies = {"http": resolved_proxy_url, "https": resolved_proxy_url}

            effective_user_agent = str(
                user_agent
                or (self.get_request_fingerprint() or {}).get("user_agent")
                or self._generate_user_agent(str(project_id or method))
            ).strip()
            if method == "twocaptcha":
                from .twocaptcha import solve_recaptcha_v3

                solution = await solve_recaptcha_v3(
                    client_key, website_url, website_key, page_action,
                    enterprise=True, base_url=base_url,
                    proxy_url=resolved_proxy_url or None,
                )
                return solution["gRecaptchaResponse"], solution["userAgent"]
            if method == "captcharun":
                headers = {
                    "Authorization": f"Bearer {client_key}",
                    "Content-Type": "application/json",
                }
                task = {
                    "captchaType": "ReCaptchaV3",
                    "siteKey": website_key,
                    "siteReferer": website_url,
                    "siteAction": page_action,
                }
                async with AsyncSession() as session:
                    request_options = {
                        "headers": headers,
                        "impersonate": "chrome",
                        "timeout": 30,
                    }
                    if proxy:
                        request_options["proxy"] = proxy
                    elif proxies:
                        request_options["proxies"] = proxies
                    result = await session.post(
                        f"{base_url.rstrip('/')}/v2/tasks",
                        json=task,
                        **request_options,
                    )
                    payload = result.json()
                    task_id = str(payload.get("taskId") or payload.get("id") or "")
                    if result.status_code >= 400 or not task_id:
                        debug_logger.log_error(
                            f"[reCAPTCHA captcharun] create task failed: {payload}"
                        )
                        return None
                    for _ in range(60):
                        await asyncio.sleep(3)
                        result = await session.get(
                            f"{base_url.rstrip('/')}/v2/tasks/{task_id}",
                            **request_options,
                        )
                        payload = result.json()
                        status = str(payload.get("status") or "").lower()
                        if status == "success":
                            solution = payload.get("response") or {}
                            token = solution.get("gRecaptchaResponse")
                            if token:
                                return str(token), effective_user_agent
                            return None
                        if status in {"failed", "failure", "error", "cancelled"}:
                            debug_logger.log_error(
                                f"[reCAPTCHA captcharun] task failed: {payload}"
                            )
                            return None
                return None

            async with AsyncSession() as session:
                create_url = f"{base_url}/createTask"
                create_data = {
                    "clientKey": client_key,
                    "task": {
                        "websiteURL": website_url,
                        "websiteKey": website_key,
                        "type": task_type,
                        "pageAction": page_action,
                    },
                }
                if is_yes_style_captcha_method(method):
                    create_data["softID"] = "33424"
                if effective_user_agent:
                    create_data["task"]["userAgent"] = effective_user_agent
                if min_score is not None:
                    create_data["task"]["minScore"] = min_score

                task_id = None
                last_create_error = None
                max_create_attempts = (
                    len(self.YESCAPTCHA_SLOT_BACKOFF_SECONDS)
                    if is_yes_style_captcha_method(method)
                    else 1
                )
                for create_attempt in range(max_create_attempts):
                    if proxy:
                        result = await session.post(
                            create_url,
                            json=create_data,
                            impersonate="chrome",
                            proxy=proxy,
                        )
                    else:
                        result = await session.post(
                            create_url,
                            json=create_data,
                            impersonate="chrome",
                            proxies=proxies,
                        )

                    debug_logger.log_info(
                        f"[reCAPTCHA {method}] createTask response status: {result.status_code}"
                    )
                    result_json = result.json()
                    task_id = result_json.get("taskId")

                    debug_logger.log_info(
                        f"[reCAPTCHA {method}] created task_id: {task_id}, response: {result_json}"
                    )

                    if task_id:
                        break

                    error_code = str(result_json.get("errorCode") or "").strip()
                    error_desc = result_json.get("errorDescription", "Unknown error")
                    last_create_error = result_json
                    if is_yes_style_captcha_method(method) and error_code in {
                        "ERROR_NO_SLOT_AVAILABLE",
                        "ERROR_NO_SLOT_AVAILABLE_BLOCK",
                    }:
                        delay = self._get_retry_delay_seconds(
                            error_code, create_attempt
                        )
                        if create_attempt < max_create_attempts - 1:
                            debug_logger.log_warning(
                                f"[reCAPTCHA {method}] createTask 资源不足 ({error_code})，将在 {delay} 秒后重试 "
                                f"({create_attempt + 2}/{max_create_attempts})"
                            )
                            await asyncio.sleep(delay)
                            continue
                    debug_logger.log_error(
                        f"[reCAPTCHA {method}] Failed to create task: {error_desc}"
                    )
                    return None

                if not task_id:
                    error_desc = (last_create_error or {}).get(
                        "errorDescription", "Unknown error"
                    )
                    debug_logger.log_error(
                        f"[reCAPTCHA {method}] Failed to create task: {error_desc}"
                    )
                    return None

                get_url = f"{base_url}/getTaskResult"
                for i in range(40):
                    get_data = {"clientKey": client_key, "taskId": task_id}
                    # 根据代理类型使用不同参数
                    if proxy:
                        result = await session.post(
                            get_url, json=get_data, impersonate="chrome", proxy=proxy
                        )
                    else:
                        result = await session.post(
                            get_url,
                            json=get_data,
                            impersonate="chrome",
                            proxies=proxies,
                        )
                    result_json = result.json()

                    debug_logger.log_info(
                        f"[reCAPTCHA {method}] polling #{i + 1}: {result_json}"
                    )

                    status = result_json.get("status")
                    if status == "ready":
                        solution = result_json.get("solution", {})
                        response = solution.get("gRecaptchaResponse")
                        if response:
                            self._merge_request_fingerprint(
                                self._build_fingerprint_from_user_agent(
                                    solution.get("userAgent"),
                                    accept_language=self._get_primary_accept_language(),
                                    proxy_url=resolved_proxy_url,
                                )
                            )
                            debug_logger.log_info(f"[reCAPTCHA {method}] Token获取成功")
                            # 同时返回 userAgent, 调用方会注入到 fingerprint context,
                            # 保证 Flow API 提交请求时使用与打码一致的 UA。
                            user_agent = solution.get("userAgent")
                            return response, user_agent

                    await asyncio.sleep(3)

                debug_logger.log_error(
                    f"[reCAPTCHA {method}] Timeout waiting for token"
                )
                return None

        except Exception as e:
            debug_logger.log_error(f"[reCAPTCHA {method}] error: {str(e)}")
            return None


class FlowClient(CurrentFlowClientMixin, _FlowClientBase):
    """Flow client backed exclusively by the current flow.google.com RPC layer."""

    @staticmethod
    def _get_runtime_config():
        return config
