"""A thin FHIR R4 client for the HAPI server in docker-compose.yaml.

Plain dicts in, plain dicts out: FHIR JSON *is* the data model, and the
server validates it (a bad code value or a reference to a missing resource
comes back as a 400 with an OperationOutcome, see below). Typed models
(`fhir.resources`) would add a large dependency to re-check what HAPI already
checks. They'd earn their keep once this app *builds* complex resources in
many places, and not before.

What every request does, and why (each from a real HAPI response; see
docs/notes/fhir-cheatsheet.md):
  * `Accept: application/fhir+json`. Without it, a malformed-body error came
    back without a JSON body.
  * `Cache-Control: no-cache` on searches. HAPI reuses the results of an
    identical search for a while: right after a DELETE, the same search
    returned no entries but still `total: 1`. Callers get entries only, never
    `total`.
  * Searches follow `next` links (up to `max_pages`). A page holds `_count`
    results, 20 by default.

Errors are raised as FhirError subclasses. Each carries the HAPI status, the
OperationOutcome issues, and `api_status`: the status *this* app should
answer with when a FHIR call fails underneath one of its own endpoints
(registered as a FastAPI exception handler in app/main.py).

Synchronous on purpose, like the DB access: call it from a worker thread
(`asyncio.to_thread`) on async paths, as the voice pipeline already does for
the database.
"""

import logging
import re
from typing import Any

import httpx

from app.config import settings

__all__ = [
    "FhirClient",
    "FhirError",
    "FhirNotFound",
    "FhirGone",
    "FhirInvalidReference",
    "FhirBadRequest",
    "FhirConflict",
    "FhirUnavailable",
]

FHIR_JSON = "application/fhir+json"

# httpx logs every request URL at INFO, and a FHIR search URL *is* patient
# data: `Patient?phone=...&family=Lee`. The log scrubber masks phone numbers
# but not names (app/phi/log_scrub.py), so request-line logging is off.
# Errors still surface: as FhirError, logged with HAPI codes only.
logging.getLogger("httpx").setLevel(logging.WARNING)
_HAPI_CODE = re.compile(r"HAPI-\d{4}")


# ── Errors: HAPI status -> this app's HTTP semantics ────────────────────────
class FhirError(Exception):
    """A FHIR request failed. `status` is HAPI's; `api_status` is ours."""

    api_status = 502

    def __init__(self, message: str, *, status: int | None = None, issues: list[dict] | None = None):
        super().__init__(message)
        self.status = status
        self.issues = issues or []

    @property
    def codes(self) -> list[str]:
        """HAPI's own message codes, all of them: diagnostics can chain
        several ("HAPI-0550: HAPI-0989: ..."). Stable, and safe to log; the
        full diagnostics text can echo request values (PHI), so it isn't."""
        return [code for i in self.issues for code in _HAPI_CODE.findall(i.get("diagnostics", ""))]


class FhirNotFound(FhirError):
    """404: no such resource (HAPI-2001), or no such resource type (HAPI-0302)."""

    api_status = 404


class FhirGone(FhirNotFound):
    """410: the resource existed and was deleted. Our API still says 404:
    a caller shouldn't learn that a record once existed."""


class FhirInvalidReference(FhirError):
    """400 HAPI-1094: the resource points at something that doesn't exist
    ("Resource Patient/x not found, specified in path: Encounter.subject").
    It's a data problem in what was submitted, so our API says 422."""

    api_status = 422


class FhirBadRequest(FhirError):
    """400 otherwise: unparseable body, invalid code value, unknown search
    parameter. We built that request, so it's our bug: 500."""

    api_status = 500


class FhirConflict(FhirError):
    """409/412: a version conflict on a conditional update (If-Match), or a
    conditional create that matched more than one resource."""

    api_status = 409


class FhirUnavailable(FhirError):
    """The FHIR server is down, timed out, or failed (5xx). Retryable."""

    api_status = 503


def _raise_for(response: httpx.Response) -> None:
    if response.is_success:
        return
    issues: list[dict] = []
    try:
        body = response.json()
        if body.get("resourceType") == "OperationOutcome":
            issues = body.get("issue", [])
    except ValueError:
        pass
    status = response.status_code
    message = f"FHIR {response.request.method} {response.request.url.path} -> {status}"
    kwargs = {"status": status, "issues": issues}
    if status == 410:
        raise FhirGone(message, **kwargs)
    if status == 404:
        raise FhirNotFound(message, **kwargs)
    if status in (409, 412):
        raise FhirConflict(message, **kwargs)
    if status == 400 and any("HAPI-1094" in i.get("diagnostics", "") for i in issues):
        raise FhirInvalidReference(message, **kwargs)
    if 400 <= status < 500:
        raise FhirBadRequest(message, **kwargs)
    raise FhirUnavailable(message, **kwargs)


# ── Client ───────────────────────────────────────────────────────────────────
class FhirClient:
    def __init__(self, base_url: str | None = None, *, http: httpx.Client | None = None, timeout: float = 10.0):
        self._http = http or httpx.Client(timeout=timeout)
        self._base = (base_url or settings.fhir_base_url).rstrip("/")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "FhirClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _request(self, method: str, url: str, **kwargs) -> dict:
        headers = {"Accept": FHIR_JSON, **kwargs.pop("headers", {})}
        if "json" in kwargs:
            headers["Content-Type"] = FHIR_JSON
            # Prefer the created/updated resource back, not an empty body.
            headers.setdefault("Prefer", "return=representation")
        try:
            response = self._http.request(method, url, headers=headers, **kwargs)
        except httpx.TransportError as exc:
            raise FhirUnavailable(f"FHIR {method} {url}: {type(exc).__name__}") from exc
        _raise_for(response)
        return response.json() if response.content else {}

    def _url(self, *parts: str) -> str:
        return "/".join([self._base, *parts])

    def create(self, resource: dict) -> dict:
        """POST /{type}. Returns the stored resource, with its server-assigned
        `id` and `meta.versionId` "1"."""
        return self._request("POST", self._url(resource["resourceType"]), json=resource)

    def read(self, resource_type: str, resource_id: str) -> dict:
        """GET /{type}/{id}. FhirNotFound if it never existed, FhirGone if deleted."""
        return self._request("GET", self._url(resource_type, resource_id))

    def update(self, resource: dict, *, if_match: str | None = None) -> dict:
        """PUT /{type}/{id}: replaces the *whole* resource (fields left out are
        gone; the old version stays readable via _history). Pass the version
        you read as `if_match` to fail with FhirConflict instead of silently
        overwriting someone else's change."""
        headers = {"If-Match": f'W/"{if_match}"'} if if_match else {}
        return self._request(
            "PUT", self._url(resource["resourceType"], resource["id"]), json=resource, headers=headers,
        )

    def search(
        self, resource_type: str, params: dict[str, Any] | list[tuple[str, str]] | None = None,
        *, max_pages: int = 10,
    ) -> list[dict]:
        """GET /{type}?params: the matching resources (not `_include`d ones),
        across pages. No match is an empty list, not an error: HAPI answers a
        search that finds nothing with 200 and an empty Bundle.

        Pass a list of pairs to repeat a parameter, which is how FHIR ANDs
        conditions: [("start", "ge2026-10-05"), ("start", "lt2026-10-12")]."""
        bundle = self._request(
            "GET", self._url(resource_type), params=params or {}, headers={"Cache-Control": "no-cache"},
        )
        resources: list[dict] = []
        for page in range(max_pages):
            resources += [
                e["resource"] for e in bundle.get("entry", [])
                if e.get("search", {}).get("mode", "match") == "match"
            ]
            next_url = next((link["url"] for link in bundle.get("link", []) if link["relation"] == "next"), None)
            if next_url is None or page == max_pages - 1:
                break
            bundle = self._request("GET", next_url, headers={"Cache-Control": "no-cache"})
        return resources

    def transaction(self, bundle: dict) -> dict:
        """POST / with a Bundle of type "transaction": all entries succeed or
        none do (a bad reference in one entry rolled back the others). Entries
        can reference each other by `urn:uuid:` fullUrl. Returns the
        transaction-response Bundle: one entry per request, in order, with
        `response.status` and `response.location`."""
        if bundle.get("type") != "transaction":
            raise ValueError('transaction() needs a Bundle with "type": "transaction"')
        return self._request("POST", self._base, json=bundle)
