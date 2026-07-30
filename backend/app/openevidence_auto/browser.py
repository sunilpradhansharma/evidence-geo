"""Playwright harness that drives the OpenEvidence web UI as a logged-in HCP.

OpenEvidence has no API and is HCP-gated, so unattended capture means automating the
real browser. Login is email+password (no MFA); the authenticated session persists in
a Playwright *persistent context* (``oe_user_data_dir``), so we seed the login once and
reuse it. Re-login happens automatically when the session lapses.

IMPORTANT — selectors are best-effort: OpenEvidence's real DOM has not been inspected
yet. Run ``python -m scripts.oe_spike`` to verify/tune them, or override via the
OE_*_SELECTOR settings. The waiting logic is deliberately selector-tolerant
(network-idle + text stabilization) so it can work even before selectors are perfect.

Playwright is imported lazily so the rest of the app never depends on it being
installed. Install with: ``pip install playwright && playwright install chromium``.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from app.config.settings import PROJECT_ROOT, Settings
from app.utils.logging import get_logger

logger = get_logger("openevidence.browser")

# --- Best-effort selector candidates (first visible match wins) -------------------
# Tune via OE_*_SELECTOR settings or scripts/oe_spike.py once the live DOM is known.
_EMAIL_CANDIDATES = [
    "input[type='email']",
    "input[name='email']",
    "input[autocomplete='email']",
    "input[name='username']",
    "input[id*='email' i]",
]
_PASSWORD_CANDIDATES = [
    "input[type='password']",
    "input[name='password']",
    "input[autocomplete='current-password']",
    "input[id*='password' i]",
]
_SUBMIT_CANDIDATES = [
    "button[type='submit']",
    "button:has-text('Sign in')",
    "button:has-text('Log in')",
    "button:has-text('Continue')",
    "button:has-text('Sign In')",
    "input[type='submit']",
]
# Controls that appear ONLY when logged OUT. OpenEvidence renders the composer to
# anonymous visitors as well, so the presence of these — not the absence of the
# composer — is the reliable "not authenticated" signal (an anonymous ask returns
# "You do not have permission to perform this action").
_LOGGED_OUT_CANDIDATES = [
    "button:has-text('Log In')",
    "button:has-text('Sign Up')",
    "a:has-text('Log In')",
    "a:has-text('Sign Up')",
]
# The entry point that OPENS the login form from the landing page (distinct from the
# in-form submit button).
_LOGIN_OPEN_CANDIDATES = [
    "button:has-text('Log In')",
    "a:has-text('Log In')",
    "button:has-text('Log in')",
    "button:has-text('Sign In')",
    "a:has-text('Sign In')",
]
_PROMPT_CANDIDATES = [
    "textarea",
    "[contenteditable='true']",
    "textarea[placeholder]",
    "input[type='search']",
    "[role='textbox']",
]
# Containers likely to hold the rendered answer; the richest-text match is chosen.
_ANSWER_CANDIDATES = [
    "[data-testid*='answer' i]",
    "[data-testid*='response' i]",
    "[class*='answer' i]",
    "[class*='response' i]",
    "[class*='markdown' i]",
    "[class*='prose' i]",
    "article",
    "main",
]

# Defensive anti-detection init script. Runs before every page load (add_init_script).
# Every patch is individually try/caught: a thrown patch would itself be a detection
# signal ("JavaScript not working"), so robustness is prioritised over completeness.
_STEALTH_JS = r"""
(() => {
  const patch = (fn) => { try { fn(); } catch (e) {} };

  patch(() => Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined }));
  patch(() => { delete navigator.__proto__.webdriver; });

  patch(() => Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] }));

  patch(() => {
    const mk = (name, filename, description) => ({ name, filename, description, length: 1 });
    const plugins = [
      mk('Chrome PDF Plugin', 'internal-pdf-viewer', 'Portable Document Format'),
      mk('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', ''),
      mk('Native Client', 'internal-nacl-plugin', ''),
    ];
    Object.defineProperty(navigator, 'plugins', { get: () => plugins });
    Object.defineProperty(navigator, 'mimeTypes', { get: () => [{ type: 'application/pdf' }] });
  });

  patch(() => {
    if (!window.chrome) window.chrome = {};
    window.chrome.runtime = window.chrome.runtime || {};
    window.chrome.app = window.chrome.app || { isInstalled: false };
    window.chrome.csi = window.chrome.csi || function () {};
    window.chrome.loadTimes = window.chrome.loadTimes || function () {};
  });

  patch(() => {
    const perms = window.navigator.permissions;
    const orig = perms && perms.query;
    if (orig) {
      perms.query = (p) => (p && p.name === 'notifications')
        ? Promise.resolve({ state: (typeof Notification !== 'undefined' ? Notification.permission : 'default') })
        : orig.call(perms, p);
    }
  });

  patch(() => {
    const spoof = (proto) => {
      if (!proto) return;
      const gp = proto.getParameter;
      proto.getParameter = function (param) {
        if (param === 37445) return 'Intel Inc.';
        if (param === 37446) return 'Intel Iris OpenGL Engine';
        return gp.apply(this, [param]);
      };
    };
    spoof(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
    spoof(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  });

  patch(() => Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 }));
  patch(() => Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 }));
  patch(() => { if (!window.outerWidth) window.outerWidth = window.innerWidth; });
  patch(() => { if (!window.outerHeight) window.outerHeight = window.innerHeight + 74; });
})();
"""

# Matches an IPv4 dotted-quad or a compact IPv6 string (loose, for the egress probe).
_IP_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$|^[0-9a-fA-F:]{2,45}$")


def parse_proxy(settings) -> dict | None:
    """Build a Playwright proxy dict from the OE_PROXY_* settings, or None if unset.

    Credentials may be supplied either as separate OE_PROXY_USERNAME/OE_PROXY_PASSWORD
    or embedded in the server URL (``scheme://user:pass@host:port``) — the form most
    residential-proxy providers hand out. Chromium ignores credentials embedded in the
    ``server`` string, so any embedded pair is split out into the dedicated
    username/password fields. A bare ``host:port`` (no scheme) defaults to ``http://``.
    The returned ``server`` never contains credentials, so it is safe to surface in
    /status.
    """
    raw = (settings.oe_proxy_server or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    server = f"{parsed.scheme}://{parsed.hostname or ''}"
    if parsed.port:
        server += f":{parsed.port}"
    username = parsed.username or (settings.oe_proxy_username or "").strip() or None
    password = parsed.password or (settings.oe_proxy_password or "") or None
    proxy: dict = {"server": server}
    if username:
        proxy["username"] = username
        proxy["password"] = password or ""
    return proxy


class OpenEvidenceError(RuntimeError):
    """Raised when the harness cannot log in, ask, or scrape an answer."""


class OpenEvidenceBrowser:
    """Async context manager around a persistent Playwright Chromium session.

    Usage:
        async with OpenEvidenceBrowser(settings) as oe:
            answer, sources = await oe.ask("What is the MOA of ...?")
    """

    def __init__(self, settings: Settings):
        self.s = settings
        self._pw = None
        self._browser = None   # remote Browser handle (Scraping Browser / CDP mode)
        self._context = None   # BrowserContext (local persistent, or remote context)
        self._page = None
        self.base_url = settings.oe_base_url.rstrip("/")
        self.login_url = settings.oe_login_url.strip() or self.base_url
        self.ask_url = settings.oe_ask_url.strip() or self.base_url
        self.user_data_dir = Path(settings.oe_user_data_dir.strip() or (PROJECT_ROOT / ".oe_session"))
        self._state_path = self.user_data_dir / "state.json"
        self._cdp_url = (settings.oe_scraping_browser_cdp or "").strip()
        self.remote = bool(self._cdp_url)  # True = connect to a remote CAPTCHA-solving browser
        self._debug_dir = PROJECT_ROOT / "exports" / "oe_debug"

    # ---- lifecycle ----------------------------------------------------------------
    async def __aenter__(self) -> "OpenEvidenceBrowser":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:  # pragma: no cover - depends on optional dep
            raise OpenEvidenceError(
                "Playwright is not installed. Run: pip install playwright && "
                "playwright install chromium"
            ) from e

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        if self.remote:
            # Bright Data's Scraping Browser wss endpoint presents a certificate chain
            # Node can't verify by default ("unable to verify the first certificate";
            # also triggered by corporate TLS inspection). In remote mode the driver's
            # ONLY TLS hop is to this trusted, credentialed endpoint (real page TLS
            # happens inside the remote browser), so relax verification for the driver.
            # Must be set BEFORE the Node driver process spawns.
            os.environ["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
        self._pw = await async_playwright().start()
        if self.remote:
            await self._start_remote()
        else:
            await self._start_local()

    async def _start_local(self) -> None:
        """Launch a LOCAL persistent Chromium (default mode). Login persists in
        oe_user_data_dir; a residential proxy (OE_PROXY_*) gives a clean IP but does not
        auto-solve a CAPTCHA."""
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self.s.oe_headless:
            args.append("--no-sandbox")  # required under Docker/root; not JS-detectable

        launch_kwargs = dict(
            user_data_dir=str(self.user_data_dir),
            headless=self.s.oe_headless,
            args=args,
            ignore_default_args=["--enable-automation"],  # drop the "controlled by automation" tell
            viewport={"width": 1366, "height": 864},
            locale=(self.s.oe_locale or "en-US"),
            timezone_id=(self.s.oe_timezone_id or "America/Chicago"),
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
            color_scheme="light",
            extra_http_headers={"Accept-Language": f"{self.s.oe_locale or 'en-US'},en;q=0.9"},
            proxy=self._proxy(),
        )
        ua = self.s.oe_user_agent.strip()
        if ua:  # blank = keep the browser's real UA (stays consistent with client hints)
            launch_kwargs["user_agent"] = ua

        channel = (self.s.oe_browser_channel or "").strip()
        try:
            self._context = await self._pw.chromium.launch_persistent_context(
                channel=channel or None, **launch_kwargs
            )
        except Exception as e:  # noqa: BLE001 - fall back to bundled chromium
            if channel:
                logger.warning("Chrome channel '%s' unavailable (%s); using bundled chromium", channel, e)
                self._context = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
            else:
                raise

        # Anti-detection: a JS init script that runs before every page load, plus an
        # optional playwright-stealth layer. Toggle the whole thing with OE_STEALTH.
        if self.s.oe_stealth:
            await self._context.add_init_script(_STEALTH_JS)
        else:
            await self._context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )

        self._context.set_default_timeout(self.s.oe_nav_timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        if self.s.oe_stealth:
            await self._maybe_apply_playwright_stealth()
        logger.info("OpenEvidence browser started (headless=%s, stealth=%s, proxy=%s, profile=%s)",
                    self.s.oe_headless, self.s.oe_stealth, bool(self._proxy()), self.user_data_dir)

    async def _start_remote(self) -> None:
        """Connect to a REMOTE CAPTCHA-solving browser (e.g. Bright Data Scraping
        Browser) over CDP. The provider manages residential IPs + anti-bot + CAPTCHA, so
        we skip local launch/stealth and persist the login as cookies (state.json)
        because the remote browser is ephemeral."""
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(
                self._cdp_url, timeout=self.s.oe_nav_timeout_ms
            )
        except Exception as e:  # noqa: BLE001
            raise OpenEvidenceError(
                f"Could not connect to the remote Scraping Browser over CDP: {e}. "
                "Check OE_SCRAPING_BROWSER_CDP (expected wss://...@host:9222)."
            ) from e
        self._context = (
            self._browser.contexts[0] if self._browser.contexts
            else await self._browser.new_context()
        )
        await self._restore_cookies()
        self._context.set_default_timeout(self.s.oe_nav_timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        logger.info("OpenEvidence connected to remote Scraping Browser over CDP (restored_session=%s)",
                    self._state_path.exists())

    async def close(self) -> None:
        try:
            if self._context is not None and not self.remote:
                await self._context.close()  # local: closes the persistent context + browser
            if self._browser is not None:
                await self._browser.close()  # remote: disconnect from the Scraping Browser
        finally:
            if self._pw is not None:
                await self._pw.stop()
            self._context = self._page = self._pw = self._browser = None

    async def _restore_cookies(self) -> None:
        """Best-effort: reload a previously saved logged-in session so the remote browser
        can skip the login flow (and avoid repeated logins that may trip OpenEvidence's
        account security)."""
        if not self._state_path.exists():
            return
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            cookies = state.get("cookies") or []
            if cookies:
                await self._context.add_cookies(cookies)
                logger.info("Restored %d saved OpenEvidence cookie(s) into the session", len(cookies))
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not restore saved session: %s", e)

    async def _persist_session(self) -> None:
        """Best-effort: save the logged-in session (cookies + storage) so the next run can
        skip login. Essential in remote mode (no local profile); harmless locally."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            state = await self._context.storage_state()
            self._state_path.write_text(json.dumps(state), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not persist session: %s", e)

    def _proxy(self) -> dict | None:
        return parse_proxy(self.s)

    async def _maybe_apply_playwright_stealth(self) -> None:
        """Best-effort: layer the optional playwright-stealth package on top of the
        built-in init script if it happens to be installed. Never required."""
        try:
            from playwright_stealth import stealth_async  # type: ignore
        except Exception:  # noqa: BLE001 - optional dependency
            return
        try:
            for pg in self._context.pages:
                await stealth_async(pg)
        except Exception as e:  # noqa: BLE001
            logger.debug("playwright-stealth not applied: %s", e)

    async def _human_pause(self, lo: float = 0.4, hi: float = 1.2) -> None:
        """Sleep a randomized, human-like interval."""
        await asyncio.sleep(random.uniform(lo, hi))

    # ---- auth ----------------------------------------------------------------------
    async def _first_visible(self, selectors: list[str], timeout_ms: int = 4000):
        """Return the first locator from `selectors` that is visible, else None."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for sel in selectors:
                try:
                    loc = self._page.locator(sel).first
                    if await loc.is_visible():
                        return loc
                except Exception:  # noqa: BLE001 - selector may be invalid/detached
                    continue
            await asyncio.sleep(0.2)
        return None

    async def is_logged_in(self) -> bool:
        """Logged in iff the composer is present AND the anonymous Log In / Sign Up
        controls are gone. The public landing page shows the composer to anonymous
        visitors too, so the composer alone is NOT proof — they are denied with
        "You do not have permission to perform this action" on submit."""
        if await self._first_visible(_LOGGED_OUT_CANDIDATES, timeout_ms=2500) is not None:
            return False
        return await self._first_visible(self._prompt_selectors(), timeout_ms=3000) is not None

    async def _dump_fields(self) -> str:
        """Best-effort summary of the visible inputs/buttons on the current page. Lets a
        failed login reveal OpenEvidence's auth method (password vs. emailed code vs.
        SSO) without a human watching the browser."""
        try:
            return await self._page.evaluate(
                """() => {
                    const vis = el => { const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
                    const inputs = [...document.querySelectorAll('input,textarea')].filter(vis)
                        .map(el => `${el.tagName.toLowerCase()}(type=${el.type||''},name=${el.name||''},ph=${JSON.stringify(el.placeholder||'')})`);
                    const buttons = [...document.querySelectorAll('button,a')].filter(vis)
                        .map(el => (el.innerText||'').replace(/\\s+/g,' ').trim())
                        .filter(t => t && t.length < 40).slice(0, 25);
                    return 'fields=' + JSON.stringify({url: location.href, inputs, buttons});
                }"""
            )
        except Exception as e:  # noqa: BLE001 - diagnostics must never mask the real error
            return f"<field dump failed: {e}>"

    async def ensure_logged_in(self) -> None:
        await self._goto(self.base_url)
        if await self.is_logged_in():
            return
        if not (self.s.oe_email and self.s.oe_password):
            await self._snapshot("login_required")
            raise OpenEvidenceError(
                "OpenEvidence session is not logged in and OE_EMAIL/OE_PASSWORD are not "
                "set. Seed the session once (POST /openevidence/auto/login or "
                "scripts/oe_spike.py)."
            )
        await self.login()

    async def login(self) -> None:
        logger.info("Logging into OpenEvidence as %s", self.s.oe_email)
        await self._goto(self.login_url)

        # OpenEvidence's landing page only exposes a "Log In" entry point; the real
        # login form (email step) opens from it. Click it unless a form is already up.
        if await self._first_visible(_EMAIL_CANDIDATES, timeout_ms=1500) is None:
            opener = await self._first_visible(_LOGIN_OPEN_CANDIDATES, timeout_ms=5000)
            if opener is not None:
                await opener.click()
                await self._human_pause(0.6, 1.4)

        email = await self._first_visible(_EMAIL_CANDIDATES, timeout_ms=10000)
        if email is None:
            await self._snapshot("login_no_email_field")
            raise OpenEvidenceError(
                f"Could not find the email field on the login page. {await self._dump_fields()}"
            )
        await email.fill(self.s.oe_email)

        # Password flows often reveal the field only after the email is submitted.
        # Passwordless (emailed code / magic-link) and SSO flows have NO password.
        pwd = await self._first_visible(_PASSWORD_CANDIDATES, timeout_ms=2000)
        if pwd is None:
            cont = await self._first_visible(_SUBMIT_CANDIDATES, timeout_ms=2500)
            if cont is not None:
                await cont.click()
                await self._human_pause(0.6, 1.4)
            pwd = await self._first_visible(_PASSWORD_CANDIDATES, timeout_ms=8000)
        if pwd is None:
            await self._snapshot("login_no_password_field")
            raise OpenEvidenceError(
                "No password field after the email step — OpenEvidence is likely using a "
                f"passwordless (emailed code / magic-link) or SSO login. {await self._dump_fields()}"
            )
        await pwd.fill(self.s.oe_password)

        submit = await self._first_visible(_SUBMIT_CANDIDATES, timeout_ms=4000)
        if submit is not None:
            await submit.click()
        else:
            await self._page.keyboard.press("Enter")

        # Success = the anonymous Log In / Sign Up controls are gone AND the composer
        # is present. Landing on a page that merely has a composer is NOT enough — it
        # renders for anonymous visitors too, who are denied at ask time.
        deadline = time.monotonic() + self.s.oe_nav_timeout_ms / 1000
        while time.monotonic() < deadline:
            if await self.is_logged_in():
                break
            await asyncio.sleep(0.5)
        else:
            await self._snapshot("login_failed")
            raise OpenEvidenceError(
                "Login did not reach an authenticated session (Log In/Sign Up still "
                "present). Likely a wrong password, a CAPTCHA/MFA, an email magic-link "
                "or SSO login flow, or an unverified (non-HCP) account — check "
                "exports/oe_debug."
            )
        logger.info("OpenEvidence login OK; session persisted in %s", self.user_data_dir)
        await self._persist_session()

    # ---- ask + scrape --------------------------------------------------------------
    async def ask(self, question: str) -> tuple[str, list[dict]]:
        """Submit one question, wait for the streamed answer, return (text, sources)."""
        await self.ensure_logged_in()
        if self.ask_url != self.base_url:
            await self._goto(self.ask_url)

        prompt = await self._first_visible(self._prompt_selectors(), timeout_ms=self.s.oe_nav_timeout_ms)
        if prompt is None:
            await self._snapshot("ask_no_prompt")
            raise OpenEvidenceError("Could not find the question/prompt box.")

        try:
            await self._page.mouse.move(random.randint(200, 900), random.randint(150, 500))
            await prompt.scroll_into_view_if_needed()
            await prompt.hover()
        except Exception:  # noqa: BLE001 - mouse/scroll/hover are best-effort niceties
            pass
        await self._human_pause(0.2, 0.6)
        await prompt.click()
        await prompt.fill("")  # clear any residual text
        await self._human_pause(0.2, 0.5)
        await prompt.press_sequentially(question, delay=random.randint(30, 90))  # human-ish typing
        await self._human_pause(0.5, 1.1)
        await self._page.keyboard.press("Enter")

        answer = await self._wait_for_answer()
        if not answer.strip():
            await self._snapshot("ask_empty_answer")
            raise OpenEvidenceError("Answer came back empty (selector or stream-detection issue).")
        sources = await self._extract_sources()
        logger.info("OpenEvidence answer captured (%d chars, %d sources)", len(answer), len(sources))
        return answer, sources

    async def _wait_for_answer(self) -> str:
        """Poll the answer container until its text stops changing (stream complete)."""
        try:
            await self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001 - streaming may keep the socket busy; that's fine
            pass

        deadline = time.monotonic() + self.s.oe_answer_timeout_ms / 1000
        stable_for = self.s.oe_answer_stable_ms / 1000
        last_text = ""
        stable_since: float | None = None
        while time.monotonic() < deadline:
            text = await self._read_answer_text()
            if text and text == last_text:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= stable_for:
                    return text
            else:
                stable_since = None
                last_text = text
            await asyncio.sleep(0.4)
        return last_text

    def _prompt_selectors(self) -> list[str]:
        override = self.s.oe_prompt_selector.strip()
        return [override] + _PROMPT_CANDIDATES if override else _PROMPT_CANDIDATES

    async def _answer_locator(self):
        """Resolve the answer element: explicit override, else richest-text candidate."""
        override = self.s.oe_answer_selector.strip()
        if override:
            loc = self._page.locator(override).last
            return loc if await loc.count() else None
        best = None
        best_len = 0
        for sel in _ANSWER_CANDIDATES:
            try:
                loc = self._page.locator(sel).last
                if not await loc.count():
                    continue
                text = await loc.inner_text()
                if len(text) > best_len:
                    best, best_len = loc, len(text)
            except Exception:  # noqa: BLE001
                continue
        return best

    async def _read_answer_text(self) -> str:
        loc = await self._answer_locator()
        if loc is None:
            return ""
        try:
            return (await loc.inner_text()).strip()
        except Exception:  # noqa: BLE001 - element may have detached mid-stream
            return ""

    async def _extract_sources(self) -> list[dict]:
        """Collect citation links (url + title) from the citation/answer region."""
        sel = self.s.oe_citation_selector.strip()
        anchors = None
        if sel:
            anchors = self._page.locator(sel)
        else:
            loc = await self._answer_locator()
            anchors = loc.locator("a[href^='http']") if loc is not None else None
        if anchors is None:
            return []

        out: list[dict] = []
        seen: set[str] = set()
        try:
            count = await anchors.count()
        except Exception:  # noqa: BLE001
            return []
        base_host = urlparse(self.base_url).netloc.lower()
        for i in range(min(count, 50)):
            a = anchors.nth(i)
            try:
                href = (await a.get_attribute("href") or "").strip()
                title = (await a.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not href.startswith("http") or href in seen:
                continue
            # Skip OpenEvidence's own internal navigation links.
            if urlparse(href).netloc.lower() == base_host:
                continue
            seen.add(href)
            out.append({"url": href, "title": title or None})
        return out

    # ---- helpers -------------------------------------------------------------------
    async def _goto(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded", timeout=self.s.oe_nav_timeout_ms)
        if self.s.oe_stealth:
            await self._human_pause(0.6, 1.6)  # natural post-load dwell

    async def _snapshot(self, label: str) -> None:
        """Best-effort debug screenshot to exports/oe_debug for selector triage."""
        try:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
            path = self._debug_dir / f"{int(time.time())}_{label}.png"
            await self._page.screenshot(path=str(path), full_page=True)
            logger.info("Saved OpenEvidence debug screenshot: %s", path)
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not save debug screenshot: %s", e)

    async def egress_ip(self) -> str | None:
        """Best-effort: the public IP this browser presents (routed through OE_PROXY_*
        if set). Lets you verify a residential proxy is actually working — and see the
        exact IP OpenEvidence sees — before blaming the anti-bot wall. Never raises."""
        for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=15000)
                text = (await self._page.inner_text("body")).strip()
                candidate = text.split()[0] if text else ""
                if candidate and _IP_RE.match(candidate):
                    return candidate
            except Exception:  # noqa: BLE001 - probe is best-effort
                continue
        return None
