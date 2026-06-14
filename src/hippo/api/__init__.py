"""Hippo HTTP API package.

This was a single api.py whose build_app() had grown into a ~770-line
god-function (routing + auth wiring + middleware + config resolution + MCP mount
+ SPA fallback all inline, ~25 route handlers as closures over a couple dozen
locals). It was decomposed (MED-04) into:

  context.py        AppContext + build_context (the dependency bundle)
  auth.py           importable/testable auth dependencies + authz helpers
  models.py         request schemas + shared constants + _safe_filename
  routes_session.py oidc/password auth + first-run wizard
  routes_account.py /health, /me, /me/password, /users*
  routes_content.py /chat, /ingest, /documents*, /folders*
  routes_admin.py   /config*, /tokens*, /settings/status
  app.py            build_app — the thin assembler

Public surface is unchanged: `from hippo.api import build_app` (and the
`_safe_filename` helper the tests import) resolve exactly as before.
"""

from .app import build_app
from .models import _safe_filename

__all__ = ["build_app", "_safe_filename"]
