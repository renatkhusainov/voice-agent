"""app/fhir/client.py.

Unit tests run on httpx.MockTransport, replaying the exact responses HAPI
8.12 gave (docs/notes/fhir-cheatsheet.md), so they need no server. One test
at the bottom runs the real round trip against HAPI and skips itself when
it isn't up (`docker compose up -d hapi`).
"""


import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.fhir.client import (
    FhirBadRequest,
    FhirClient,
    FhirConflict,
    FhirGone,
    FhirInvalidReference,
    FhirNotFound,
    FhirUnavailable,
)
from app.main import fhir_error

BASE = "http://fhir.test/fhir"


def outcome(diagnostics: str) -> dict:
    return {"resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": "processing", "diagnostics": diagnostics}]}


def client_answering(handler) -> tuple[FhirClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)
    return FhirClient(BASE, http=httpx.Client(transport=httpx.MockTransport(record))), seen


# ── Requests ─────────────────────────────────────────────────────────────────
def test_create_posts_fhir_json_and_returns_the_stored_resource():
    stored = {"resourceType": "Patient", "id": "1000", "meta": {"versionId": "1"}}
    fhir, seen = client_answering(lambda r: httpx.Response(201, json=stored))

    result = fhir.create({"resourceType": "Patient", "name": [{"family": "Smith"}]})

    assert result == stored
    [request] = seen
    assert (request.method, str(request.url)) == ("POST", f"{BASE}/Patient")
    assert request.headers["Content-Type"] == "application/fhir+json"
    assert request.headers["Accept"] == "application/fhir+json"
    assert request.headers["Prefer"] == "return=representation"


def test_update_puts_to_the_resource_url_with_if_match():
    fhir, seen = client_answering(lambda r: httpx.Response(200, json={"id": "1000", "meta": {"versionId": "3"}}))

    fhir.update({"resourceType": "Patient", "id": "1000"}, if_match="2")

    assert (seen[0].method, str(seen[0].url)) == ("PUT", f"{BASE}/Patient/1000")
    assert seen[0].headers["If-Match"] == 'W/"2"'


def test_search_bypasses_hapis_search_cache_and_follows_next_links():
    pages = {
        f"{BASE}/Patient?family=Smith": {
            "resourceType": "Bundle", "type": "searchset", "total": 3,
            "link": [{"relation": "next", "url": f"{BASE}?_getpages=abc&_getpagesoffset=2"}],
            "entry": [{"resource": {"id": "1"}, "search": {"mode": "match"}},
                      {"resource": {"id": "2"}, "search": {"mode": "match"}},
                      {"resource": {"id": "org-9"}, "search": {"mode": "include"}}],
        },
        f"{BASE}?_getpages=abc&_getpagesoffset=2": {
            "resourceType": "Bundle", "type": "searchset",
            "entry": [{"resource": {"id": "3"}, "search": {"mode": "match"}}],
        },
    }
    fhir, seen = client_answering(lambda r: httpx.Response(200, json=pages[str(r.url)]))

    result = fhir.search("Patient", {"family": "Smith"})

    assert [r["id"] for r in result] == ["1", "2", "3"]  # _include'd resources are not matches
    assert all(r.headers["Cache-Control"] == "no-cache" for r in seen)


def test_search_can_repeat_a_parameter_for_a_range():
    fhir, seen = client_answering(lambda r: httpx.Response(200, json={"resourceType": "Bundle"}))

    fhir.search("Slot", [("status", "free"), ("start", "ge2026-10-05"), ("start", "lt2026-10-12")])

    assert seen[0].url.params.get_list("start") == ["ge2026-10-05", "lt2026-10-12"]


def test_an_empty_search_is_an_empty_list_not_an_error():
    fhir, _ = client_answering(lambda r: httpx.Response(200, json={"resourceType": "Bundle", "type": "searchset", "total": 0}))

    assert fhir.search("Patient", {"family": "Nobody"}) == []


def test_search_stops_after_max_pages():
    looping = {"resourceType": "Bundle", "entry": [{"resource": {"id": "x"}}],
               "link": [{"relation": "next", "url": f"{BASE}?_getpages=again"}]}
    fhir, seen = client_answering(lambda r: httpx.Response(200, json=looping))

    assert len(fhir.search("Patient", max_pages=3)) == 3
    assert len(seen) == 3


def test_transaction_posts_to_the_base_and_requires_a_transaction_bundle():
    fhir, seen = client_answering(lambda r: httpx.Response(200, json={"resourceType": "Bundle", "type": "transaction-response"}))

    fhir.transaction({"resourceType": "Bundle", "type": "transaction", "entry": []})
    assert (seen[0].method, str(seen[0].url)) == ("POST", BASE)
    with pytest.raises(ValueError):
        fhir.transaction({"resourceType": "Bundle", "type": "batch", "entry": []})


# ── Errors: HAPI's real responses -> exception -> our HTTP status ────────────
@pytest.mark.parametrize("status, body, error, api_status", [
    (404, outcome("HAPI-2001: Resource Patient/999999 is not known"), FhirNotFound, 404),
    (404, outcome("HAPI-0302: Unknown resource type 'Toothbrush'"), FhirNotFound, 404),
    (410, outcome("Resource was deleted at 2026-09-30T15:54:36.808+00:00"), FhirGone, 404),
    (400, outcome("HAPI-1094: Resource Patient/does-not-exist not found, specified in path: Encounter.subject"),
     FhirInvalidReference, 422),
    (400, outcome('HAPI-0524: Unknown search parameter "favourite_color" for resource type "Patient"'), FhirBadRequest, 500),
    (400, outcome("HAPI-0450: Failed to parse request body as JSON resource. Error was: HAPI-1821: ..."), FhirBadRequest, 500),
    (409, outcome("HAPI-0550: HAPI-0989: Trying to update Patient/1000/_history/1 but this is not the current version"),
     FhirConflict, 409),
    (500, outcome("HAPI-9999: something broke"), FhirUnavailable, 503),
])
def test_hapi_errors_map_to_our_http_semantics(status, body, error, api_status):
    fhir, _ = client_answering(lambda r: httpx.Response(status, json=body))

    with pytest.raises(error) as raised:
        fhir.read("Patient", "999999")

    assert raised.value.status == status
    assert raised.value.api_status == api_status


def test_chained_hapi_codes_are_all_reported():
    fhir, _ = client_answering(lambda r: httpx.Response(409, json=outcome(
        "HAPI-0550: HAPI-0989: Trying to update Patient/1000/_history/1 but this is not the current version")))

    with pytest.raises(FhirConflict) as raised:
        fhir.update({"resourceType": "Patient", "id": "1000"}, if_match="1")

    assert raised.value.codes == ["HAPI-0550", "HAPI-0989"]


def test_server_unreachable_is_unavailable():
    def refuse(request):
        raise httpx.ConnectError("connection refused")
    fhir, _ = client_answering(refuse)

    with pytest.raises(FhirUnavailable):
        fhir.read("Patient", "1")


def test_api_answers_with_the_mapped_status_and_no_diagnostics_text():
    # The diagnostics echo request values; the API body must not.
    api = FastAPI()
    api.add_exception_handler(FhirInvalidReference, fhir_error)
    diagnostics = "HAPI-1094: Resource Patient/jane-smith-1985 not found, specified in path: Appointment.participant"

    @api.get("/boom")
    def boom():
        raise FhirInvalidReference("x", status=400, issues=outcome(diagnostics)["issue"])

    response = TestClient(api).get("/boom")

    assert response.status_code == 422
    assert response.json() == {"detail": "FhirInvalidReference", "fhir_status": 400, "fhir_codes": ["HAPI-1094"]}
    assert "jane-smith" not in response.text


# ── Live round trip (skipped without HAPI) ───────────────────────────────────
def _hapi_up() -> bool:
    try:
        return httpx.get("http://localhost:8090/fhir/metadata", timeout=2).status_code == 200
    except Exception:  # not reachable, for whatever reason: skip, don't error
        return False


@pytest.mark.skipif(not _hapi_up(), reason="HAPI not running: docker compose up -d hapi")
def test_live_create_read_search_update_delete():
    with FhirClient("http://localhost:8090/fhir") as fhir:
        family = "Livetest"
        created = fhir.create({"resourceType": "Patient", "name": [{"family": family, "given": ["Robin"]}]})
        try:
            assert fhir.read("Patient", created["id"])["meta"]["versionId"] == "1"
            assert created["id"] in [p["id"] for p in fhir.search("Patient", {"family": family})]
            updated = fhir.update({**created, "gender": "unknown"}, if_match="1")
            assert updated["meta"]["versionId"] == "2"
            with pytest.raises(FhirConflict):
                fhir.update({**created, "gender": "other"}, if_match="1")
        finally:
            httpx.delete(f"http://localhost:8090/fhir/Patient/{created['id']}")
        with pytest.raises(FhirGone):
            fhir.read("Patient", created["id"])
        assert created["id"] not in [p["id"] for p in fhir.search("Patient", {"family": family})]
