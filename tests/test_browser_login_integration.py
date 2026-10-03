"""Opt-in Chromium/Xvfb tests against a local fake Twitch service, never real credentials."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("TDM_BROWSER_TESTS") != "1",
    reason="Set TDM_BROWSER_TESTS=1 with Chromium, Xvfb and noVNC installed.",
)


def test_real_browser_delegation_and_mfa(monkeypatch):
    import zendriver as zd
    from aiohttp import web
    from fastapi import FastAPI

    from constants import ClientType
    from webui import browser_display, browser_login

    requests = []
    reports = []
    native_payload = browser_login.delegated_payload
    native_pattern = zd.cdp.fetch.RequestPattern
    monkeypatch.setattr(browser_display, "app", FastAPI())

    async def run():
        async def page(request):
            return web.Response(
                content_type="text/html",
                text="""<h1>Local login fixture</h1>
<script>
addEventListener('load', async () => {
  const payload = {username: 'fixture', password: 'fixture-password'};
  await fetch('/protected_login', {method:'POST',body:JSON.stringify(payload)});
  payload.authy_token = '123456';
  await fetch('/protected_login', {method:'POST',body:JSON.stringify(payload)});
});
</script>""",
            )

        async def authenticate(request):
            payload = await request.json()
            requests.append(payload)
            if "authy_token" not in payload:
                return web.json_response({"error_code": 3011}, status=400)
            return web.json_response({"access_token": "fixture-android-token"})

        async def validate(request):
            assert request.headers["Authorization"] == "OAuth fixture-android-token"
            return web.json_response(
                {"client_id": ClientType.ANDROID_APP.CLIENT_ID, "user_id": "42"}
            )

        service = web.Application()
        service.add_routes(
            [
                web.get("/login", page),
                web.post("/protected_login", authenticate),
                web.get("/validate", validate),
            ]
        )
        runner = web.AppRunner(service)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        # Route the real browser through the same interception code using a local
        # provider. No fake passwords or tokens ever go to Twitch.
        monkeypatch.setattr(browser_login, "LOGIN_URL", base + "/login")
        monkeypatch.setattr(browser_login, "VALIDATE_URL", base + "/validate")
        monkeypatch.setattr(browser_login, "LOGIN_TIMEOUT", 30)
        monkeypatch.setattr(
            browser_login,
            "delegated_payload",
            lambda url, method, body: native_payload(
                url.replace(base, "https://passport.twitch.tv", 1),
                method,
                body,
            ),
        )
        monkeypatch.setattr(
            zd.cdp.fetch,
            "RequestPattern",
            lambda **kwargs: native_pattern(
                url_pattern=base + "/protected_login*",
            ),
        )
        try:
            async with browser_display.BrowserDisplay() as display:
                token = await browser_login.browser_login(
                    display.display, asyncio.Event(), reports.append
                )
                assert token == "fixture-android-token"
            assert not display.view_url
            assert not display._processes
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert len(requests) == 2
    assert all(p["client_id"] == ClientType.WEB.CLIENT_ID for p in requests)
    assert all(
        p["delegate_client_id"] == ClientType.ANDROID_APP.CLIENT_ID for p in requests
    )
    assert requests[1]["authy_token"] == "123456"
    assert any("3011" in r for r in reports)
    assert any("validated" in r for r in reports)
    assert "fixture-password" not in json.dumps(reports)
    assert "fixture-android-token" not in json.dumps(reports)


def test_viewer_websocket_is_scoped_to_active_attempt(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from webui import browser_display

    api = FastAPI()
    monkeypatch.setattr(browser_display, "app", api)

    async def run():
        display = browser_display.BrowserDisplay()
        with TestClient(api) as client:
            assert client.get("/browser-login/view/expired").status_code == 410
            async with display:
                view_url = display.view_url
                assert client.get(view_url).status_code == 200
                assert client.get("/browser-login/novnc/core/rfb.js").status_code == 200
                socket_url = view_url.replace("/view/", "/socket/")
                with (
                    pytest.raises(WebSocketDisconnect),
                    client.websocket_connect(
                        socket_url, headers={"origin": "https://untrusted.example"}
                    ),
                ):
                    pass
                with (
                    pytest.raises(WebSocketDisconnect),
                    client.websocket_connect(
                        "/browser-login/socket/wrong-key",
                        headers={"origin": "http://testserver"},
                    ),
                ):
                    pass
                with client.websocket_connect(
                    socket_url, headers={"origin": "http://testserver"}
                ) as socket:
                    version = socket.receive_bytes()
                    assert version.startswith(b"RFB ")
                    socket.send_bytes(version)
                    assert socket.receive_bytes()
            assert client.get(view_url).status_code == 410
            with (
                pytest.raises(WebSocketDisconnect),
                client.websocket_connect(
                    socket_url, headers={"origin": "http://testserver"}
                ),
            ):
                pass

    asyncio.run(run())
