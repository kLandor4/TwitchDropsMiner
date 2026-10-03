"""Exercise delegation, token boundaries, and cancellation without Twitch credentials."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from constants import ClientType
from webui import browser_login as login


@pytest.mark.parametrize(
    "path", ["/protected_login", "/protected_login/shim?trusted_request=true"]
)
def test_delegation_preserves_password_and_mfa(path):
    original = {
        "username": "test-account",
        "password": "test-password",
        "remember_me": True,
        "authy_token": "123456",
        "twitchguard_code": "654321",
        "captcha": {"proof": "proof"},
        "client_id": ClientType.WEB.CLIENT_ID,
    }
    body = login.delegated_payload(
        "https://passport.twitch.tv" + path, "POST", json.dumps(original)
    )
    assert json.loads(body) == {
        **original,
        "delegate_client_id": ClientType.ANDROID_APP.CLIENT_ID,
    }


@pytest.mark.parametrize(
    "url,method",
    [
        ("https://passport.twitch.tv/protected_login", "OPTIONS"),
        ("https://passport.twitch.tv/protected_register", "POST"),
        ("https://passport.twitch.tv/protected_login/unknown", "POST"),
        ("https://passport.twitch.tv.evil.example/protected_login", "POST"),
        ("http://passport.twitch.tv/protected_login", "POST"),
        ("https://gql.twitch.tv/gql", "POST"),
    ],
)
def test_other_requests_are_untouched(url, method):
    assert login.delegated_payload(url, method, "not-json") is None


@pytest.mark.parametrize("body", [None, "not-json", "[]", '{"password":"do-not-log"}'])
def test_unrecognized_login_stops_without_exposing_body(body):
    with pytest.raises(login.BrowserLoginError) as exc:
        login.delegated_payload(
            "https://passport.twitch.tv/protected_login", "POST", body
        )
    assert "do-not-log" not in str(exc.value)


def mock_validation(monkeypatch, data, status=200):
    response = SimpleNamespace(status=status, json=AsyncMock(return_value=data))
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    request.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get.return_value = request
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(login.aiohttp, "ClientSession", MagicMock(return_value=context))
    return session


def test_android_token_is_validated_without_logging_it(monkeypatch):
    session = mock_validation(
        monkeypatch, {"client_id": ClientType.ANDROID_APP.CLIENT_ID, "user_id": "42"}
    )
    reports = []
    asyncio.run(login.validate_android_token("private-test-token", reports.append))
    session.get.assert_called_once_with(
        login.VALIDATE_URL,
        headers={"Authorization": "OAuth private-test-token"},
        allow_redirects=False,
    )
    assert "ANDROID_APP" in reports[-1]
    assert "private-test-token" not in " ".join(reports)


@pytest.mark.parametrize("client_id", [ClientType.WEB.CLIENT_ID, "other-client", None])
def test_non_android_token_is_rejected(monkeypatch, client_id):
    mock_validation(monkeypatch, {"client_id": client_id, "user_id": "42"})
    with pytest.raises(login.BrowserLoginError, match="not Android"):
        asyncio.run(login.validate_android_token("test-token", MagicMock()))


@pytest.mark.parametrize(
    "data", [[], {}, {"client_id": ClientType.ANDROID_APP.CLIENT_ID}]
)
def test_incomplete_validation_cannot_login(monkeypatch, data):
    mock_validation(monkeypatch, data)
    with pytest.raises(login.BrowserLoginError):
        asyncio.run(login.validate_android_token("test-token", MagicMock()))


def test_rejected_validation_cannot_login(monkeypatch):
    mock_validation(monkeypatch, {}, status=401)
    with pytest.raises(login.BrowserLoginError, match="HTTP 401"):
        asyncio.run(login.validate_android_token("test-token", MagicMock()))


def fake_browser(monkeypatch, navigate):
    """Use the browser API boundary, leaving the complete async login flow intact."""

    def command(name):
        return lambda *args, **kwargs: (name, args, kwargs)

    fetch = SimpleNamespace(
        RequestPaused=object(),
        RequestPattern=SimpleNamespace,
        **{
            name: command(name)
            for name in (
                "enable",
                "continue_request",
                "get_response_body",
                "fail_request",
            )
        },
    )
    tab = SimpleNamespace(closed=False, commands=[], body="", callback=None)

    async def send(cmd):
        tab.commands.append(cmd)
        if cmd[0] == "get_response_body":
            return tab.body, False

    def add_handler(_event, callback):
        tab.callback = callback

    async def get(url):
        await navigate(tab)
        return tab

    tab.send = send
    tab.add_handler = add_handler
    tab.get = get
    browser = SimpleNamespace(
        stopped=False,
        start=AsyncMock(),
        stop=AsyncMock(),
        get=AsyncMock(return_value=tab),
    )
    module = SimpleNamespace(
        Config=lambda **kw: kw,
        Browser=lambda config: browser,
        cdp=SimpleNamespace(
            fetch=fetch,
            network=SimpleNamespace(ErrorReason=SimpleNamespace(ABORTED="Aborted")),
        ),
    )
    monkeypatch.setitem(sys.modules, "zendriver", module)
    return browser, tab


def login_event(request_id, status=None):
    return SimpleNamespace(
        request_id=request_id,
        response_status_code=status,
        response_error_reason=None,
        request=SimpleNamespace(
            url="https://passport.twitch.tv/protected_login",
            method="POST",
            post_data=json.dumps(
                {"username": "test", "password": "never-log", "authy_token": "123456"}
            ),
        ),
    )


def test_full_login_handles_mfa_and_captures_only_its_response(monkeypatch):
    async def navigate(tab):
        for request_id, status, result in [
            ("one", 400, {"error_code": 3011}),
            ("unrelated", 200, {"access_token": "wrong-token"}),
            ("two", 200, {"access_token": "right-token"}),
        ]:
            if request_id != "unrelated":
                await tab.callback(login_event(request_id))
            tab.body = json.dumps(result)
            await tab.callback(login_event(request_id, status))

    browser, tab = fake_browser(monkeypatch, navigate)
    validator = AsyncMock()
    monkeypatch.setattr(login, "validate_android_token", validator)
    reports = []

    async def run():
        return await login.browser_login(":123", asyncio.Event(), reports.append)

    assert asyncio.run(run()) == "right-token"
    validator.assert_awaited_once_with("right-token", reports.append)
    browser.stop.assert_awaited_once()
    submissions = [
        c for c in tab.commands if c[0] == "continue_request" and "post_data" in c[2]
    ]
    assert len(submissions) == 2
    for _, _, kwargs in submissions:
        body = json.loads(base64.b64decode(kwargs["post_data"]))
        assert body["client_id"] == ClientType.WEB.CLIENT_ID
        assert body["delegate_client_id"] == ClientType.ANDROID_APP.CLIENT_ID
        assert body["authy_token"] == "123456"
        assert kwargs["intercept_response"] is True
    assert any("3011" in r for r in reports)
    assert not any(
        secret in " ".join(reports)
        for secret in ("never-log", "right-token", "wrong-token")
    )


@pytest.mark.parametrize("cancel_mode", ["button", "shutdown", "timeout"])
def test_browser_is_closed_on_cancellation(monkeypatch, cancel_mode):
    ready = None

    async def navigate(tab):
        ready.set()

    browser, _ = fake_browser(monkeypatch, navigate)
    if cancel_mode == "timeout":
        monkeypatch.setattr(login, "LOGIN_TIMEOUT", 0.02)

    async def run():
        nonlocal ready
        ready = asyncio.Event()
        cancel = asyncio.Event()
        task = asyncio.create_task(login.browser_login(":123", cancel, lambda _: None))
        await ready.wait()
        if cancel_mode == "button":
            cancel.set()
        elif cancel_mode == "shutdown":
            task.cancel()
        with pytest.raises(
            asyncio.CancelledError
            if cancel_mode == "shutdown"
            else login.BrowserLoginError
        ):
            await task

    asyncio.run(run())
    browser.stop.assert_awaited_once()


def test_browser_failure_does_not_log_protocol_payload(monkeypatch):
    async def navigate(tab):
        raise RuntimeError("password=secret-token=secret")

    browser, _ = fake_browser(monkeypatch, navigate)

    async def run():
        with pytest.raises(login.BrowserLoginError) as exc:
            await login.browser_login(":123", asyncio.Event(), lambda _: None)
        assert "secret" not in str(exc.value)

    asyncio.run(run())
    browser.stop.assert_awaited_once()
