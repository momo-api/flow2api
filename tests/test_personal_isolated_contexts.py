import json
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from nodriver import cdp

import src.services.browser_captcha_personal as bcp
from src.services.browser_captcha_personal import (
    BrowserCaptchaService,
    ResidentTabInfo,
    clear_cached_session_cookies,
    get_cached_session_cookies,
)


COOKIES_TOKEN_1 = json.dumps(
    [
        {"name": "SID", "value": "sid-1", "domain": ".google.com", "path": "/", "secure": True},
        {"name": "OSID", "value": "osid-1", "domain": "flow.google.com", "path": "/", "secure": True},
    ]
)
COOKIES_TOKEN_2 = json.dumps(
    [
        {"name": "SID", "value": "sid-2", "domain": ".google.com", "path": "/", "secure": True},
        {"name": "OSID", "value": "osid-2", "domain": "flow.google.com", "path": "/", "secure": True},
    ]
)


def _make_fake_tab(*, target_id="target-1", browser_context_id="ctx-1", url="about:blank"):
    tab = MagicMock()
    tab.type_ = "page"
    tab.target_id = target_id
    tab.url = url
    tab.target = types.SimpleNamespace(
        target_id=target_id,
        browser_context_id=browser_context_id,
    )
    tab.send = AsyncMock(return_value=[])
    tab.sleep = AsyncMock()
    tab.close = AsyncMock()
    tab.reload = AsyncMock()
    tab.evaluate = AsyncMock(return_value="complete")
    return tab


class PersonalIsolatedContextsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_cached_session_cookies()

    def tearDown(self):
        clear_cached_session_cookies()

    def test_use_isolated_browser_contexts_env_and_headless_matrix(self):
        uninitialized = BrowserCaptchaService.__new__(BrowserCaptchaService)
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(uninitialized._use_isolated_browser_contexts())

        headed = BrowserCaptchaService()
        headed.headless = False
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(headed._use_isolated_browser_contexts())
        with patch.dict("os.environ", {"PERSONAL_BROWSER_ISOLATED_CONTEXTS": "0"}, clear=True):
            self.assertFalse(headed._use_isolated_browser_contexts())
        with patch.dict("os.environ", {"PERSONAL_BROWSER_ISOLATED_CONTEXTS": "1"}, clear=True):
            self.assertTrue(headed._use_isolated_browser_contexts())
        with patch.dict("os.environ", {"PERSONAL_BROWSER_ISOLATED_CONTEXTS": "true"}, clear=True):
            self.assertTrue(headed._use_isolated_browser_contexts())

        headless = BrowserCaptchaService()
        headless.headless = True
        with patch.dict("os.environ", {"PERSONAL_BROWSER_ISOLATED_CONTEXTS": "0"}, clear=True):
            self.assertTrue(headless._use_isolated_browser_contexts())

    async def test_headed_create_isolated_contexts_tracked_and_skipped_as_host_page(self):
        service = BrowserCaptchaService()
        service.headless = False
        host_tab = _make_fake_tab(target_id="host-target", browser_context_id=None)
        tab_ctx_1 = _make_fake_tab(target_id="t-1", browser_context_id="ctx-token-1")
        tab_ctx_2 = _make_fake_tab(target_id="t-2", browser_context_id="ctx-token-2")

        ctx_ids = iter(["ctx-token-1", "ctx-token-2"])
        target_ids = iter(["t-1", "t-2"])
        targets = [host_tab]

        async def fake_send(cmd):
            payload = next(cmd)
            method = payload.get("method")
            if method == "Target.createBrowserContext":
                return next(ctx_ids)
            if method == "Target.createTarget":
                tid = next(target_ids)
                if tid == "t-1":
                    targets.append(tab_ctx_1)
                else:
                    targets.append(tab_ctx_2)
                return tid
            if method == "Target.disposeBrowserContext":
                return None
            return None

        fake_browser = types.SimpleNamespace(
            stopped=False,
            targets=targets,
            send=AsyncMock(side_effect=fake_send),
            update_targets=AsyncMock(),
        )
        service.browser = fake_browser
        service._apply_tab_startup_spoofs = AsyncMock()
        service._apply_configured_browser_startup_cookie = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"PERSONAL_BROWSER_ISOLATED_CONTEXTS": "1"}, clear=True):
            got_tab_1, got_ctx_1 = await service._create_isolated_context_tab(
                "about:blank", label="token1"
            )
            got_tab_2, got_ctx_2 = await service._create_isolated_context_tab(
                "about:blank", label="token2"
            )

            self.assertIs(got_tab_1, tab_ctx_1)
            self.assertIs(got_tab_2, tab_ctx_2)
            self.assertEqual(got_ctx_1, "ctx-token-1")
            self.assertEqual(got_ctx_2, "ctx-token-2")
            self.assertEqual(
                service._get_isolated_browser_context_ids(),
                {"ctx-token-1", "ctx-token-2"},
            )

            # Move isolated tabs ahead of host_tab to prove host lookup skips them
            fake_browser.targets = [tab_ctx_1, tab_ctx_2, host_tab]
            picked_host = await service._ensure_browser_host_page(label="check_host")
            self.assertIs(picked_host, host_tab)

            await service._dispose_browser_context_quietly("ctx-token-1")
            self.assertEqual(
                service._get_isolated_browser_context_ids(),
                {"ctx-token-2"},
            )

            await service._shutdown_browser_runtime_locked("test_shutdown")
            self.assertEqual(service._get_isolated_browser_context_ids(), set())

    async def test_headed_isolated_binding_injects_each_token_and_clears_on_switch(self):
        service = BrowserCaptchaService()
        service.headless = False
        service.browser = types.SimpleNamespace(stopped=False, send=AsyncMock())
        service._set_browser_cookie_targets = AsyncMock(return_value=2)
        service._tab_reload = AsyncMock()
        service._remember_token_affinity = MagicMock()

        async def load_cookie(token_id):
            return COOKIES_TOKEN_1 if token_id == 1 else COOKIES_TOKEN_2

        service._load_token_cookie = AsyncMock(side_effect=load_cookie)

        tab_1 = _make_fake_tab(target_id="t-1", browser_context_id="ctx-1")
        tab_2 = _make_fake_tab(target_id="t-2", browser_context_id="ctx-2")
        resident_1 = ResidentTabInfo(
            tab=tab_1, slot_id="slot-1", token_id=None, browser_context_id="ctx-1"
        )
        resident_2 = ResidentTabInfo(
            tab=tab_2, slot_id="slot-2", token_id=None, browser_context_id="ctx-2"
        )

        with patch.dict(
            "os.environ",
            {
                "PERSONAL_BROWSER_ISOLATED_CONTEXTS": "1",
                "PERSONAL_BROWSER_USER_DATA_DIR": "/app/data/manual-flow-login",
                "PERSONAL_BROWSER_PRESERVE_PROFILE_COOKIES": "1",
            },
            clear=True,
        ):
            ok1 = await service._apply_token_cookie_binding(
                resident_1, 1, label="bind_t1", acquire_lock=False
            )
            ok2 = await service._apply_token_cookie_binding(
                resident_2, 2, label="bind_t2", acquire_lock=False
            )

            self.assertTrue(ok1)
            self.assertTrue(ok2)
            self.assertEqual(resident_1.token_id, 1)
            self.assertEqual(resident_2.token_id, 2)
            # First-time binding does not clear; each token writes into its own context
            service.browser.send.assert_not_awaited()
            calls = service._set_browser_cookie_targets.await_args_list
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0].kwargs["browser_context_id"], "ctx-1")
            self.assertEqual(calls[1].kwargs["browser_context_id"], "ctx-2")

            # Switching resident_1 from token 1 -> token 2 must clear ctx-1 first
            ok_switch = await service._apply_token_cookie_binding(
                resident_1, 2, label="switch_to_t2", acquire_lock=False
            )
            self.assertTrue(ok_switch)
            self.assertEqual(resident_1.token_id, 2)
            service.browser.send.assert_awaited_once()
            clear_cmd = service.browser.send.await_args.args[0]
            payload = next(clear_cmd)
            self.assertEqual(payload["method"], "Storage.clearCookies")
            self.assertEqual(payload["params"]["browserContextId"], "ctx-1")

    async def test_ensure_resident_binding_empty_cookies_clears_only_own_context(self):
        service = BrowserCaptchaService()
        service.headless = False
        service.browser = types.SimpleNamespace(stopped=False, send=AsyncMock())
        service._load_token_cookie = AsyncMock(return_value="")
        service._tab_reload = AsyncMock()
        service._wait_for_document_ready = AsyncMock(return_value=True)
        service._wait_for_recaptcha = AsyncMock(return_value=True)

        tab = _make_fake_tab(target_id="t-1", browser_context_id="ctx-owned-1")
        resident = ResidentTabInfo(
            tab=tab,
            slot_id="slot-1",
            token_id=1,
            browser_context_id="ctx-owned-1",
        )
        resident.cookie_signature = "old-sig"

        with patch.dict("os.environ", {"PERSONAL_BROWSER_ISOLATED_CONTEXTS": "1"}, clear=True):
            ok = await service._ensure_resident_token_binding(
                resident, 2, label="clear_test"
            )

        self.assertTrue(ok)
        self.assertEqual(resident.browser_context_id, "ctx-owned-1")
        service.browser.send.assert_awaited_once()
        tab.send.assert_not_awaited()
        payload = next(service.browser.send.await_args.args[0])
        self.assertEqual(payload["method"], "Storage.clearCookies")
        self.assertEqual(payload["params"]["browserContextId"], "ctx-owned-1")

    async def test_get_browser_cookies_no_global_fallback_and_persist_ownership_guard(self):
        service = BrowserCaptchaService()
        service.headless = False
        service.browser = types.SimpleNamespace(
            send=AsyncMock(side_effect=RuntimeError("context read boom")),
            cookies=types.SimpleNamespace(get_all=AsyncMock(return_value=[{"name": "LEAK"}])),
        )

        with patch.dict("os.environ", {"PERSONAL_BROWSER_ISOLATED_CONTEXTS": "1"}, clear=True):
            with self.assertRaises(RuntimeError):
                await service._get_browser_cookies(
                    label="isolated_read",
                    browser_context_id="ctx-1",
                )
        service.browser.cookies.get_all.assert_not_awaited()

        # Non-isolated mode keeps legacy global fallback
        with patch.dict("os.environ", {}, clear=True):
            res = await service._get_browser_cookies(
                label="legacy_read",
                browser_context_id="ctx-1",
            )
        self.assertEqual(res, [{"name": "LEAK"}])
        service.browser.cookies.get_all.assert_awaited_once()

        # Ownership guard rejects writing slot owned by token 1 into token 2
        service.db = types.SimpleNamespace(update_token=AsyncMock())
        service._get_browser_cookies = AsyncMock(return_value=[])
        resident_owned_by_1 = ResidentTabInfo(
            tab=_make_fake_tab(),
            slot_id="slot-1",
            token_id=1,
            browser_context_id="ctx-1",
        )
        persisted = await service._persist_context_cookies_to_token(
            resident_owned_by_1,
            2,
            label="mismatch_test",
        )
        self.assertFalse(persisted)
        service._get_browser_cookies.assert_not_awaited()
        service.db.update_token.assert_not_awaited()

    async def test_isolated_session_cookie_cache_does_not_leak_across_tokens(self):
        service = BrowserCaptchaService()
        service.headless = False

        sent_methods = []

        async def fake_browser_send(cmd):
            payload = next(cmd)
            sent_methods.append((payload.get("method"), payload.get("params", {})))
            if payload.get("params", {}).get("browserContextId") == "ctx-2":
                return [
                    cdp.network.Cookie.from_json(
                        {
                            "name": "SID",
                            "value": "sid-from-ctx-2",
                            "domain": ".google.com",
                            "path": "/",
                            "expires": -1,
                            "size": 10,
                            "httpOnly": False,
                            "secure": True,
                            "session": True,
                            "priority": "Medium",
                            "sameParty": False,
                            "sourceScheme": "Secure",
                            "sourcePort": 443,
                        }
                    )
                ]
            return []

        service.browser = types.SimpleNamespace(
            stopped=False,
            send=AsyncMock(side_effect=fake_browser_send),
        )
        tab_2 = _make_fake_tab(target_id="t-2", browser_context_id="ctx-2")
        resident_2 = ResidentTabInfo(
            tab=tab_2,
            slot_id="slot-2",
            token_id=2,
            browser_context_id="ctx-2",
        )

        with (
            patch.object(bcp, "_recaptcha_session_cookies", {"SID": "stale-token1-sid"}),
            patch.object(bcp, "_recaptcha_session_cookies_fetched_at", time.time()),
            patch.dict("os.environ", {"PERSONAL_BROWSER_ISOLATED_CONTEXTS": "1"}, clear=True),
        ):
            got = await service._cache_session_cookies_for_computed(resident_2)
            bundle = service._build_solve_bundle(
                token="tok",
                project_id="proj-1",
                action="IMAGE_GENERATION",
                token_id=2,
                slot_id=None,
            )
            cached_after = get_cached_session_cookies()

        self.assertEqual(got, {"SID": "sid-from-ctx-2"})
        self.assertEqual(
            sent_methods,
            [("Storage.getCookies", {"browserContextId": "ctx-2"})],
        )
        tab_2.send.assert_not_awaited()
        # Process-global cache must retain Token #1 value, untouched by Token #2
        self.assertEqual(cached_after, {"SID": "stale-token1-sid"})
        # Solve bundle bound to token_id=2 with no slot_id must NOT grab Token #1 global cache
        self.assertIsNone(bundle["session_cookies"])
