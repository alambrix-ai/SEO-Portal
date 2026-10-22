"""Headless Chromium, the production renderer (research R1).

The browser is driven through Playwright's sync API, which is not
thread-safe, so each browser lives on its own dedicated thread that owns its
``sync_playwright()`` from start to finish. Callers hand a job to that
thread and wait for the result; the browser never runs on the caller's
thread, so it can never run on a FastAPI request thread either. The same
code serves a Celery render worker (one lazily started browser per worker
process, recycled with the process) and a dev worker without Celery.

Each render is shut off from the network and the file system, in layers:

1. JavaScript is off and service workers are blocked for the context.
2. Every request the page makes is routed: ``data:`` continues, everything
   else is aborted (and counted, for tests and logs).
3. The browser resolves no host names at all
   (``--host-resolver-rules=MAP * ~NOTFOUND``) and the context is offline.
4. The page carries its own CSP (``compose.DOCUMENT_CSP``).
5. Every asset is already inlined as ``data:`` by the composer.

Each render gets a fresh browser context, a timeout from
``CREATIVE_RENDER_TIMEOUT_SECONDS`` and a slot from a semaphore sized by
``CREATIVE_MAX_CONCURRENT_RENDERS``. HTML larger than
``compose.MAX_HTML_BYTES`` is refused before it reaches the browser.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from app.core.config import settings
from app.core.logging import get_logger, log_failure
from app.services.creative.compose import MAX_HTML_BYTES, ComposeError, Layout, render_html
from app.services.creative.render.base import (
    OutputKind,
    RenderBusy,
    RenderedFile,
    RenderError,
    RendererUnavailable,
    RenderInputError,
    RenderTimeout,
    check_pdf,
    check_png,
)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Playwright, Route

log = get_logger(__name__)

#: Launch flags. The resolver rule makes every host name fail to resolve, so
#: even a request that escaped the router could not reach anything.
BASE_LAUNCH_ARGS: tuple[str, ...] = (
    "--host-resolver-rules=MAP * ~NOTFOUND",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-sync",
    "--no-first-run",
    "--metrics-recording-only",
    "--disable-dev-shm-usage",
)
#: After a failed launch, wait this long before trying again, so a missing
#: browser does not cost a launch attempt per render.
LAUNCH_RETRY_SECONDS = 60.0
#: Extra time a caller waits beyond the browser's own timeouts (launch plus
#: page load plus capture), before giving up on the job.
_GRACE_SECONDS = 15.0


def launch_args(*, sandbox: bool) -> list[str]:
    args = list(BASE_LAUNCH_ARGS)
    if not sandbox:
        args.append("--no-sandbox")
    return args


@dataclass(slots=True)
class _Job:
    kind: Literal["render", "probe"]
    future: Future[Any]
    release: Callable[[], None]
    html: str = ""
    output: OutputKind = "png"
    viewport: tuple[int, int] = (1, 1)
    #: Requests the router aborted, for logs and tests.
    blocked: list[str] = field(default_factory=list)


_STOP = object()


class _BrowserThread(threading.Thread):
    """Owns one Playwright driver and one browser, for its whole life."""

    def __init__(self, owner: ChromiumRenderer, jobs: queue.Queue[Any]) -> None:
        super().__init__(name="creative-chromium", daemon=True)
        self._owner = owner
        self._jobs = jobs
        self._browser: Browser | None = None

    def run(self) -> None:
        try:
            # Imported here, inside the try: a missing or broken Playwright
            # fails the queued jobs with RendererUnavailable (so the caller
            # falls back to Pillow) instead of leaving them to time out.
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                self._serve(pw)
        except Exception as exc:  # noqa: BLE001 - the driver itself failed; jobs are failed below
            log_failure(log, "Creative renderer: Playwright driver failed", exc)
            self._owner._launch_failed()
            self._fail_pending()
        finally:
            self._owner._thread_exited(self)

    def _serve(self, pw: Playwright) -> None:
        try:
            while True:
                job = self._jobs.get()
                if job is _STOP:
                    return
                self._run_job(pw, job)
        finally:
            if self._browser is not None and self._browser.is_connected():
                try:
                    self._browser.close()
                except Exception as exc:  # noqa: BLE001 - best-effort shutdown
                    log_failure(log, "Creative renderer: browser close failed", exc)

    def _fail_pending(self) -> None:
        """Fail queued jobs after the driver died; stop signals meant for
        other threads are put back."""
        stops = 0
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            if job is _STOP:
                stops += 1
                continue
            if job.future.set_running_or_notify_cancel():
                job.future.set_exception(RendererUnavailable())
            job.release()
        for _ in range(stops):
            self._jobs.put(_STOP)

    def _run_job(self, pw: Playwright, job: _Job) -> None:
        try:
            if not job.future.set_running_or_notify_cancel():
                return
            try:
                browser = self._ensure_browser(pw)
                result = None if job.kind == "probe" else self._render(browser, job)
            except BaseException as exc:  # noqa: BLE001 - handed to the waiting caller
                job.future.set_exception(exc)
            else:
                job.future.set_result(result)
        finally:
            job.release()

    def _ensure_browser(self, pw: Playwright) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        from playwright.sync_api import Error as PlaywrightError

        sandbox = settings.creative_chromium_sandbox
        try:
            self._browser = pw.chromium.launch(
                headless=True,
                chromium_sandbox=sandbox,
                args=launch_args(sandbox=sandbox),
                timeout=settings.creative_render_timeout_seconds * 1000,
            )
        except PlaywrightError as exc:
            self._browser = None
            log_failure(log, "Creative renderer: Chromium would not launch", exc,
                        level=logging.ERROR)
            self._owner._launch_failed()
            raise RendererUnavailable() from exc
        self._owner._launch_ok()
        return self._browser

    def _render(self, browser: Browser, job: _Job) -> bytes:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        timeout_ms = settings.creative_render_timeout_seconds * 1000
        width, height = job.viewport

        def route(r: Route) -> None:
            if r.request.url.startswith("data:"):
                r.continue_()
                return
            job.blocked.append(r.request.url[:200])
            r.abort("blockedbyclient")

        context = browser.new_context(
            java_script_enabled=False,
            service_workers="block",
            offline=True,
            accept_downloads=False,
            viewport={"width": width, "height": height},
            device_scale_factor=1,
        )
        try:
            context.set_default_timeout(timeout_ms)
            context.set_default_navigation_timeout(timeout_ms)
            context.route("**/*", route)
            page = context.new_page()
            page.set_content(job.html, wait_until="load", timeout=timeout_ms)
            if job.output == "png":
                return page.screenshot(
                    type="png",
                    clip={"x": 0, "y": 0, "width": width, "height": height},
                    animations="disabled",
                    caret="hide",
                    timeout=timeout_ms,
                )
            return page.pdf(prefer_css_page_size=True, print_background=True)
        except PlaywrightTimeout as exc:
            raise RenderTimeout() from exc
        except PlaywrightError as exc:
            log_failure(log, "Creative renderer: page failed", exc)
            if not browser.is_connected():
                self._browser = None
            raise RenderError() from exc
        finally:
            if job.blocked:
                log.warning("Creative renderer: blocked %d request(s)", len(job.blocked))
            try:
                context.close()
            except Exception as exc:  # noqa: BLE001 - the browser may already be gone
                log_failure(log, "Creative renderer: context close failed", exc)


class ChromiumRenderer:
    """Renders layouts with headless Chromium on dedicated browser threads.

    Blocking. Call it from a Celery task or a worker thread, never from an
    ``async`` request handler.
    """

    name: Literal["chromium"] = "chromium"
    measure: Literal["chromium"] = "chromium"

    def __init__(self, *, max_concurrent: int | None = None) -> None:
        self._max = max(1, max_concurrent or settings.creative_max_concurrent_renders)
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self._pid = os.getpid()
        self._slots = threading.BoundedSemaphore(self._max)
        self._jobs: queue.Queue[Any] = queue.Queue()
        self._threads: list[_BrowserThread] = []
        self._busy = 0
        self._failed_at: float | None = None
        self._launched = False
        #: URLs blocked during the most recent render (tests and diagnostics).
        self.last_blocked: tuple[str, ...] = ()

    # ── status, reported by the threads ──
    def _launch_failed(self) -> None:
        with self._lock:
            self._failed_at = time.monotonic()
            self._launched = False

    def _launch_ok(self) -> None:
        with self._lock:
            self._failed_at = None
            self._launched = True

    def _thread_exited(self, thread: _BrowserThread) -> None:
        with self._lock:
            if thread in self._threads:
                self._threads.remove(thread)

    @property
    def recently_failed(self) -> bool:
        failed = self._failed_at
        return failed is not None and time.monotonic() - failed < LAUNCH_RETRY_SECONDS

    @property
    def launched(self) -> bool:
        return self._launched

    # ── jobs ──
    def _wait_seconds(self) -> float:
        return settings.creative_render_timeout_seconds * 3 + _GRACE_SECONDS

    def _submit(self, job_kind: Literal["render", "probe"], **fields: Any) -> _Job:  # noqa: ANN401
        with self._lock:
            if os.getpid() != self._pid:
                # Forked (Celery prefork): threads do not survive a fork.
                self._reset()
        if not self._slots.acquire(timeout=settings.creative_render_timeout_seconds):
            raise RenderBusy()
        released = threading.Event()

        def release() -> None:
            if not released.is_set():
                released.set()
                with self._lock:
                    self._busy -= 1
                self._slots.release()

        job = _Job(kind=job_kind, future=Future(), release=release, **fields)
        try:
            with self._lock:
                self._busy += 1
                # Queued before a thread starts, so a thread that fails at
                # once (no Playwright, no browser) still finds and fails it.
                self._jobs.put(job)
                if len(self._threads) < min(self._busy, self._max):
                    thread = _BrowserThread(self, self._jobs)
                    self._threads.append(thread)
                    thread.start()
        except BaseException:
            release()
            raise
        return job

    def _wait(self, job: _Job) -> Any:  # noqa: ANN401
        try:
            return job.future.result(timeout=self._wait_seconds())
        except FutureTimeout as exc:
            # The slot stays held until the browser thread finishes the job,
            # so a stuck render still counts against the cap.
            job.future.cancel()
            raise RenderTimeout() from exc

    def start(self) -> None:
        """Launch the browser now, or raise :class:`RendererUnavailable`."""
        if self.recently_failed:
            raise RendererUnavailable()
        self._wait(self._submit("probe"))

    def render(self, layout: Layout, *, output: OutputKind) -> RenderedFile:
        if layout.measured_for != "chromium":
            raise RenderError()
        if self.recently_failed:
            raise RendererUnavailable()
        try:
            html = render_html(layout, output=output)
        except ComposeError as exc:
            raise RenderInputError(str(exc)) from exc
        return self.render_html(html, layout, output=output)

    def render_html(self, html: str, layout: Layout, *, output: OutputKind) -> RenderedFile:
        """Render an already-composed document. ``html`` must come from
        :func:`compose.render_html`; it is size-checked again here."""
        if len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise RenderInputError()
        spec = layout.spec
        job = self._submit("render", html=html, output=output,
                           viewport=(spec.width_px, spec.height_px))
        data = self._wait(job)
        self.last_blocked = tuple(job.blocked)
        if output == "png":
            return check_png(data, layout, "chromium")
        return check_pdf(data, layout, "chromium")

    def shutdown(self, timeout: float = 10.0) -> None:
        """Close every browser. Best effort; for worker shutdown and tests."""
        with self._lock:
            if os.getpid() != self._pid:
                return
            threads = list(self._threads)
        for _ in threads:
            self._jobs.put(_STOP)
        for thread in threads:
            thread.join(timeout)
        with self._lock:
            self._launched = False


__all__ = ["BASE_LAUNCH_ARGS", "ChromiumRenderer", "launch_args"]
