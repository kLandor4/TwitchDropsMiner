from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from translate import _

if TYPE_CHECKING:
    from yarl import URL
    from webui.manager import WebUIManager


@dataclass
class LoginData:
    username: str
    password: str
    token: str


class LoginFormAdapter:
    """
    Mirrors LoginForm and handles device-code or experimental browser login.
    """

    def __init__(self, manager: "WebUIManager"):
        self._manager = manager
        self._confirm = asyncio.Event()
        self.page_url: "URL | None" = None
        self.browser_login_enabled = (
            os.environ.get("WEBUI_TWITCH_LOGIN", "android-browser") == "android-browser"
        )
        self._browser_cancel = asyncio.Event()
        if self.browser_login_enabled:
            from webui.browser_display import BrowserDisplay

            self._browser_display = BrowserDisplay()

    def clear(self, login: bool = False, password: bool = False, token: bool = False):
        pass

    async def wait_for_login_press(self) -> None:
        self._confirm.clear()
        await self._manager.coro_unless_closed(self._confirm.wait())

    async def ask_login(self) -> LoginData:
        """Deprecated login flow; device-code flow is required."""
        return LoginData("", "", "")

    async def ask_enter_code(self, page_url: "URL", user_code: str) -> None:
        """Show the login button and wait for the user to click it before polling begins."""
        self.page_url = page_url
        self.update(_("gui", "login", "required"), None)
        self._manager.grab_attention(sound=False)
        self._manager.print(_("gui", "login", "request"))
        await self.wait_for_login_press()

    def confirm(self) -> None:
        """Signal that the user has pressed the login button."""
        self._confirm.set()

    def cancel_browser_login(self) -> None:
        self._browser_cancel.set()

    async def ask_browser_login(self) -> str:
        return await self._manager.coro_unless_closed(self._browser_login_loop())

    async def _browser_login_loop(self) -> str:
        from webui.browser_display import BrowserDisplayError
        from webui.browser_login import BrowserLoginError, browser_login

        def report(message: str) -> None:
            self._manager.print(message)
            self._manager.main_panel.update_browser_login(message=message)

        self.page_url = None
        while True:
            self._confirm.clear()
            self.update(_("gui", "login", "required"), None)
            await self._confirm.wait()
            self._browser_cancel.clear()
            self.update(_("gui", "login", "logging_in"), None)
            try:
                async with self._browser_display as display:
                    self._manager.main_panel.update_browser_login(url=display.view_url)
                    return await browser_login(
                        display.display, self._browser_cancel, report
                    )
            except (BrowserDisplayError, BrowserLoginError) as exc:
                report(str(exc))
            finally:
                self._manager.main_panel.update_browser_login(url="")

    def update(self, status: str, user_id: int | None):
        self._manager.main_panel.update_login(status, user_id)
        # Mirror login state to the status bar when the main loop hasn't set it yet
        login_statuses = (
            _("gui", "login", "logging_in"),
            _("gui", "login", "required"),
            _("gui", "login", "logged_out"),
        )
        if status in login_statuses:
            self._manager.status.update(status)
