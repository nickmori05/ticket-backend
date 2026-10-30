"""The unsubscribe pages that notification emails link to. They need no API token: the signed link is the credential.

GET shows a confirmation button and changes nothing, so a link scanner or prefetching mail client cannot unsubscribe
anyone. POST suppresses the address, which is also what a mail client sends for RFC 8058 one-click unsubscribe.
"""

from html import escape
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from ticket_backend.unsubscribe import find_unsubscribe_ticket, unsubscribe


logger = logging.getLogger(__name__)
# The token in the URL is a credential, so keep it out of caches, Referer headers, and search engines.
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
}
INVALID = ("Link not valid", "This unsubscribe link is not valid. It may be incomplete, or it may come from an "
                             "older email; use the link in your most recent email instead.")
LINK_RESPONSES = {404: {"description": "The link is malformed, forged, or no longer valid, or unsubscribe links "
                                       "are not configured", "content": {"text/html": {}}}}


def page(title: str, text: str, *, status: int = 200, button: bool = False) -> HTMLResponse:
    form = ('<form method="post"><input type="hidden" name="List-Unsubscribe" value="One-Click">'
            '<button type="submit">Unsubscribe</button></form>') if button else ""
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title>'
            f'<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem}}</style>'
            f'</head><body><h1>{escape(title)}</h1><p>{escape(text)}</p>{form}</body></html>')
    return HTMLResponse(html, status_code=status, headers=PAGE_HEADERS)


def unsubscribe_routes(app: FastAPI, database: Path, secret: str | None) -> None:
    """Add GET and POST /unsubscribe/{token}. Without a secret, every link is reported as not valid."""

    @app.get("/unsubscribe/{token}", response_class=HTMLResponse, responses=LINK_RESPONSES,
             summary="Confirm unsubscribing from notification emails")
    async def confirm(token: str):
        if secret is None or await run_in_threadpool(find_unsubscribe_ticket, token, secret, database) is None:
            return page(*INVALID, status=404)
        return page("Unsubscribe from ticket emails",
                    "Stop receiving emails about your support tickets at the address this message was sent to?",
                    button=True)

    @app.post("/unsubscribe/{token}", response_class=HTMLResponse, responses=LINK_RESPONSES,
              summary="Unsubscribe from notification emails (RFC 8058 one-click)")
    async def confirmed(token: str):
        # Any body is accepted: mail clients send List-Unsubscribe=One-Click, and the confirmation form does too.
        suppression = None if secret is None else await run_in_threadpool(unsubscribe, token, secret, database)
        if suppression is None:
            return page(*INVALID, status=404)
        logger.info("Suppressed notification emails from an unsubscribe link (suppression #%d).", suppression["id"])
        return page("Unsubscribed", "You will no longer receive emails about your support tickets. If this was a "
                                    "mistake, ask the support team to remove the suppression.")
