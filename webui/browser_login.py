"""Experimental Twitch web login with Android client delegation."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from constants import ClientType

LOGIN_URL = "https://www.twitch.tv/login"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
LOGIN_TIMEOUT = 600


class BrowserLoginError(Exception):
    pass


def browser_executable() -> str | None:
    if configured := os.environ.get("WEBUI_BROWSER_PATH"):
        return configured
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        if installed := shutil.which(name):
            return installed
    cache = Path(
        os.environ.get(
            "PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright")
        )
    )
    candidates = list(cache.glob("chromium-*/chrome-linux*/chrome"))
    return str(max(candidates, key=lambda p: p.stat().st_mtime)) if candidates else None


def delegated_payload(url: str, method: str, body: str | None) -> str | None:
    """Change only password-login requests, including subsequent MFA submissions."""
    target = urlsplit(url)
    if (
        target.scheme != "https"
        or target.netloc != "passport.twitch.tv"
        or target.path not in ("/protected_login", "/protected_login/shim")
        or method != "POST"
    ):
        return None
    try:
        payload = json.loads(body or "")
    except (ValueError, TypeError):
        raise BrowserLoginError(
            "Twitch's login request was not JSON; login stopped."
        ) from None
    if (
        not isinstance(payload, dict)
        or "username" not in payload
        or "password" not in payload
    ):
        raise BrowserLoginError(
            "Twitch's login request format changed; login stopped."
        )
    payload["client_id"] = ClientType.WEB.CLIENT_ID
    payload["delegate_client_id"] = ClientType.ANDROID_APP.CLIENT_ID
    return json.dumps(payload, separators=(",", ":"))


async def validate_android_token(token: str, report: Callable[[str], None]) -> None:
    # Keep the candidate out of the miner's request logger and cookie jar until
    # Twitch confirms its client ID. A WEB token must not invalidate saved cookies.
    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                cookie_jar=aiohttp.DummyCookieJar(),
            ) as session,
            session.get(
                VALIDATE_URL,
                headers={"Authorization": f"OAuth {token}"},
                allow_redirects=False,
            ) as response,
        ):
            if response.status != 200:
                raise BrowserLoginError(
                    f"Twitch rejected token validation (HTTP {response.status})."
                )
            data = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise BrowserLoginError(
            "Could not validate the login token with Twitch. Please try again."
        ) from None
    if not isinstance(data, dict):
        raise BrowserLoginError(
            "Twitch returned an unexpected token validation response."
        )
    client_id = data.get("client_id")
    if client_id != ClientType.ANDROID_APP.CLIENT_ID:
        kind = "WEB" if client_id == ClientType.WEB.CLIENT_ID else "a different client"
        raise BrowserLoginError(
            f"Twitch issued a token for {kind}, not Android. It was not saved."
        )
    if not str(data.get("user_id", "")).isdigit():
        raise BrowserLoginError("Twitch returned a token without a valid user ID.")
    report("Twitch validated the token as ANDROID_APP. Returning it to the miner.")


async def browser_login(
    display: str,
    cancel: asyncio.Event,
    report: Callable[[str], None],
) -> str:
    try:
        import zendriver as zd
    except ImportError:
        raise BrowserLoginError(
            "Install the WebUI dependencies with uv sync --group nicegui."
        ) from None

    # CDP debug messages contain complete login requests and responses.
    logging.getLogger("zendriver").setLevel(logging.WARNING)
    browser = None
    tasks: list[asyncio.Task] = []
    outcome = asyncio.get_running_loop().create_future()
    intercepted: set[str] = set()

    def fail(message: str) -> None:
        if not outcome.done():
            outcome.set_exception(BrowserLoginError(message))

    async def login_in_browser() -> str:
        nonlocal browser
        report(
            "Starting a private browser. Enter your password and any verification code on Twitch."
        )
        config = zd.Config(
            browser_executable_path=browser_executable(),
            headless=False,
            browser_args=[
                f"--display={display}",
                "--ozone-platform=x11",
                "--window-size=1280,900",
            ],
        )
        browser = zd.Browser(config)
        await browser.start()
        tab = await browser.get("about:blank")

        async def paused(event) -> None:
            request_id = event.request_id
            try:
                if (
                    event.response_status_code is None
                    and event.response_error_reason is None
                ):
                    body = delegated_payload(
                        event.request.url, event.request.method, event.request.post_data
                    )
                    if body is None:
                        await tab.send(zd.cdp.fetch.continue_request(request_id))
                        return
                    intercepted.add(str(request_id))
                    await tab.send(
                        zd.cdp.fetch.continue_request(
                            request_id,
                            post_data=base64.b64encode(body.encode()).decode(),
                            intercept_response=True,
                        )
                    )
                    report(
                        "Submitted browser login with WEB client_id and Android delegate_client_id."
                    )
                    return
                candidate = None
                if str(request_id) in intercepted:
                    intercepted.discard(str(request_id))
                    if event.response_error_reason is not None:
                        report(
                            "The login request failed to reach Twitch. You can retry in the browser."
                        )
                    elif event.response_status_code not in (301, 302, 303, 307, 308):
                        body, encoded = await tab.send(
                            zd.cdp.fetch.get_response_body(request_id)
                        )
                        if encoded:
                            body = base64.b64decode(body).decode()
                        try:
                            result = json.loads(body)
                        except ValueError:
                            result = {}
                        if isinstance(result, dict):
                            token = result.get("access_token")
                            if (
                                event.response_status_code == 200
                                and isinstance(token, str)
                                and token
                            ):
                                candidate = token
                            else:
                                code = result.get("error_code")
                                suffix = (
                                    f", Twitch error {code}"
                                    if isinstance(code, int)
                                    else ""
                                )
                                report(
                                    f"Login response: HTTP {event.response_status_code}{suffix}. Continue in the browser."
                                )
                await tab.send(zd.cdp.fetch.continue_request(request_id))
                if candidate and not outcome.done():
                    outcome.set_result(candidate)
            except BrowserLoginError as exc:
                with contextlib.suppress(Exception):
                    await tab.send(
                        zd.cdp.fetch.fail_request(
                            request_id, zd.cdp.network.ErrorReason.ABORTED
                        )
                    )
                fail(str(exc))
            except Exception as exc:  # noqa: BLE001 -- CDP errors may contain credentials.
                with contextlib.suppress(Exception):
                    await tab.send(zd.cdp.fetch.continue_request(request_id))
                # Protocol exceptions may include request bodies. Report only their type.
                fail(
                    f"Could not inspect the browser login response ({type(exc).__name__})."
                )

        tab.add_handler(zd.cdp.fetch.RequestPaused, paused)
        await tab.send(
            zd.cdp.fetch.enable(
                patterns=[
                    zd.cdp.fetch.RequestPattern(
                        url_pattern="https://passport.twitch.tv/protected_login*"
                    ),
                ]
            )
        )
        await tab.get(LOGIN_URL)
        report(
            "Complete Twitch login in the browser below using your password and verification code."
        )
        while not outcome.done():
            if browser.stopped or tab.closed:
                raise BrowserLoginError(
                    "The login browser was closed. Click Login to try again."
                )
            await asyncio.wait({outcome}, timeout=0.5)
        token = outcome.result()
        report("Login returned a token. Checking whether Twitch issued it for Android…")
        await validate_android_token(token, report)
        return token

    try:
        login_task = asyncio.create_task(login_in_browser())
        cancel_task = asyncio.create_task(cancel.wait())
        tasks = [login_task, cancel_task]
        done, _ = await asyncio.wait(
            tasks, timeout=LOGIN_TIMEOUT, return_when=asyncio.FIRST_COMPLETED
        )
        if cancel_task in done:
            raise BrowserLoginError("Browser login cancelled.")
        if login_task not in done:
            raise BrowserLoginError(
                "Browser login timed out after 10 minutes. Click Login to try again."
            )
        return await login_task
    except BrowserLoginError:
        raise
    except Exception as exc:  # noqa: BLE001 -- Do not expose browser protocol payloads.
        raise BrowserLoginError(
            f"Browser login could not run ({type(exc).__name__}). Check WEBUI_BROWSER_PATH and browser dependencies."
        ) from None
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not outcome.done():
            outcome.cancel()
        elif not outcome.cancelled():
            outcome.exception()
        if browser is not None:
            # Zendriver 0.17 leaves its Popen pipes open and cancels listeners
            # without awaiting them. Retain and drain these before the next attempt.
            process = getattr(browser, "_process", None)
            connections = [
                getattr(browser, "connection", None),
                *getattr(browser, "targets", []),
            ]
            listeners = [
                connection.listener.task
                for connection in connections
                if connection is not None
                and connection.listener is not None
                and connection.listener.task is not None
            ]
            try:
                await browser.stop()
            except Exception as exc:  # noqa: BLE001 -- Do not expose browser protocol payloads.
                report(f"Browser cleanup failed ({type(exc).__name__}).")
            finally:
                for listener in listeners:
                    if not listener.done():
                        listener.cancel()
                await asyncio.gather(*listeners, return_exceptions=True)
                if process is not None:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
