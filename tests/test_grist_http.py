"""HTTP-level tests for src.grist.GristClient against a faked `requests.request`.

grist_client calls `requests.request` through the module, so replacing that one
attribute fakes Grist for the shared client, also inside the worker thread the
facade runs it in. GristClient reads settings in __init__, so every test
monkeypatches the src.settings.settings singleton BEFORE constructing the client.
The two endpoints exercised are:
    GET https://grist.test/api/docs/DOC/tables/Glazes/records
    GET https://grist.test/api/docs/DOC/tables/GlazeMakers/records
"""

import json

import pytest
import requests

from src.grist import GLAZE_TABLE, MAKERS_TABLE, GristClient, GristError
from tests.conftest import make_glaze_record

BASE = "https://grist.test"
DOC = "DOC"
FOR_PRINT_URL = f"{BASE}/api/docs/{DOC}/tables/{GLAZE_TABLE}/records"
MAKERS_URL = f"{BASE}/api/docs/{DOC}/tables/{MAKERS_TABLE}/records"


def _configure(monkeypatch, *, api_key="KEY"):
    """Point the settings singleton at the test Grist instance, then return a client."""
    from src.settings import settings
    monkeypatch.setattr(settings, "grist_base_url", BASE)
    monkeypatch.setattr(settings, "grist_doc_id", DOC)
    monkeypatch.setattr(settings, "grist_api_key", api_key)
    return GristClient()


def _response(body, status_code=200):
    """A real requests.Response; a str body is sent as is, anything else as JSON."""
    response = requests.Response()
    response.status_code = status_code
    response._content = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
    return response


def _fake_grist(monkeypatch, routes):
    """Route `requests.request` by URL to a response, or to an exception it raises.

    Returns the list of calls made, each as a dict of the method, the URL and the
    keyword arguments.
    """
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        if url not in routes:
            raise AssertionError(f"unexpected Grist request: {method} {url}")
        outcome = routes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(requests, "request", fake_request)
    return calls


# ── _records ─────────────────────────────────────────────────────────────────

async def test_records_without_api_key_raises_before_any_http(monkeypatch):
    """Empty GRIST_API_KEY must raise GristError without issuing any request."""
    calls = _fake_grist(monkeypatch, {FOR_PRINT_URL: _response({"records": []})})
    client = _configure(monkeypatch, api_key="")

    with pytest.raises(GristError, match="GRIST_API_KEY is not configured"):
        await client._records(GLAZE_TABLE)

    assert calls == []


async def test_records_non_200_raises_with_body_text(monkeypatch):
    """A 500 response surfaces the status and (truncated) body in the error."""
    _fake_grist(monkeypatch, {FOR_PRINT_URL: _response("boom internal error", 500)})
    client = _configure(monkeypatch)

    with pytest.raises(GristError, match="Grist returned 500") as exc:
        await client._records(GLAZE_TABLE)

    assert "boom internal error" in str(exc.value)


async def test_records_transport_error_raises_gristerror(monkeypatch):
    """A connection-level failure is wrapped as GristError, not leaked as a requests error."""
    _fake_grist(monkeypatch, {FOR_PRINT_URL: requests.ConnectionError("refused")})
    client = _configure(monkeypatch)

    with pytest.raises(GristError, match="Grist request failed"):
        await client._records(GLAZE_TABLE)


async def test_records_missing_records_key_raises(monkeypatch):
    """A 200 whose JSON lacks the 'records' key is treated as an unexpected response."""
    _fake_grist(monkeypatch, {FOR_PRINT_URL: _response({"oops": True})})
    client = _configure(monkeypatch)

    with pytest.raises(GristError, match="Unexpected Grist response"):
        await client._records(GLAZE_TABLE)


async def test_records_returns_records_list(monkeypatch):
    """A well-formed 200 returns the list under the 'records' key verbatim."""
    records = [make_glaze_record(record_id=1), make_glaze_record(record_id=2, GeneralName="Other")]
    _fake_grist(monkeypatch, {FOR_PRINT_URL: _response({"records": records})})
    client = _configure(monkeypatch)

    result = await client._records(GLAZE_TABLE)

    assert result == records


async def test_records_sends_bearer_authorization_header(monkeypatch):
    """The API key is sent as a Bearer Authorization header."""
    calls = _fake_grist(monkeypatch, {FOR_PRINT_URL: _response({"records": []})})
    client = _configure(monkeypatch)

    await client._records(GLAZE_TABLE)

    assert calls
    assert calls[-1]["headers"]["Authorization"] == "Bearer KEY"


# ── find_glaze ───────────────────────────────────────────────────────────────

async def test_find_glaze_exact_match_preferred_over_substring(monkeypatch):
    """An exact case-insensitive match wins even when a substring candidate is earlier."""
    records = [
        # substring candidate appears FIRST in table order
        make_glaze_record(record_id=1, GeneralName="Лазурит Синий Глубокий", Maker=None),
        # exact (modulo case) candidate appears LATER
        make_glaze_record(record_id=2, GeneralName="лазурит синий", Maker=None),
    ]
    _fake_grist(monkeypatch, {FOR_PRINT_URL: _response({"records": records})})
    client = _configure(monkeypatch)

    fields = await client.find_glaze("Лазурит Синий")

    assert fields is not None
    assert fields["GeneralName"] == "лазурит синий"


async def test_find_glaze_substring_fallback_when_no_exact(monkeypatch):
    """With no exact match, the first case-insensitive substring match is returned."""
    records = [
        make_glaze_record(record_id=1, GeneralName="Изумруд Зелёный", Maker=None),
        make_glaze_record(record_id=2, GeneralName="Лазурит Синий Глубокий", Maker=None),
    ]
    _fake_grist(monkeypatch, {FOR_PRINT_URL: _response({"records": records})})
    client = _configure(monkeypatch)

    fields = await client.find_glaze("лазурит")

    assert fields is not None
    assert fields["GeneralName"] == "Лазурит Синий Глубокий"


async def test_find_glaze_no_match_returns_none(monkeypatch):
    """A query that matches nothing returns None."""
    records = [make_glaze_record(record_id=1, GeneralName="Изумруд Зелёный", Maker=None)]
    _fake_grist(monkeypatch, {FOR_PRINT_URL: _response({"records": records})})
    client = _configure(monkeypatch)

    assert await client.find_glaze("нет такого") is None


async def test_find_glaze_trims_query_whitespace(monkeypatch):
    """Leading/trailing whitespace in the query is trimmed before matching."""
    records = [make_glaze_record(record_id=1, GeneralName="Лазурит Синий", Maker=None)]
    _fake_grist(monkeypatch, {FOR_PRINT_URL: _response({"records": records})})
    client = _configure(monkeypatch)

    fields = await client.find_glaze("   Лазурит Синий   ")

    assert fields is not None
    assert fields["GeneralName"] == "Лазурит Синий"


async def test_find_glaze_resolves_maker_link_from_makers_table(monkeypatch):
    """The matched row's Maker ref is resolved into a Makerlink via GlazeMakers."""
    for_print = [make_glaze_record(record_id=1, GeneralName="Лазурит Синий", Maker=7)]
    makers = [
        {"id": 3, "fields": {"Makerlink": "https://example.com/wrong"}},
        {"id": 7, "fields": {"Makerlink": "https://example.com/maker-7"}},
    ]
    _fake_grist(monkeypatch, {
        FOR_PRINT_URL: _response({"records": for_print}),
        MAKERS_URL: _response({"records": makers}),
    })
    client = _configure(monkeypatch)

    fields = await client.find_glaze("Лазурит Синий")

    assert fields is not None
    assert fields["Makerlink"] == "https://example.com/maker-7"


# ── _maker_link ──────────────────────────────────────────────────────────────

async def test_maker_link_none_ref_returns_empty_without_http(monkeypatch):
    """A missing/None Maker ref short-circuits to "" and does not hit GlazeMakers."""
    calls = _fake_grist(monkeypatch, {MAKERS_URL: _response({"records": []})})
    client = _configure(monkeypatch)

    assert await client._maker_link(None) == ""
    assert calls == []


async def test_maker_link_unknown_id_returns_empty(monkeypatch):
    """A Maker id absent from GlazeMakers resolves to an empty string."""
    makers = [{"id": 1, "fields": {"Makerlink": "https://example.com/a"}}]
    _fake_grist(monkeypatch, {MAKERS_URL: _response({"records": makers})})
    client = _configure(monkeypatch)

    assert await client._maker_link(999) == ""


async def test_maker_link_tagged_reference_form_resolves(monkeypatch):
    """The tagged ['R', '<Table>', <rowId>] reference form is resolved too."""
    makers = [{"id": 7, "fields": {"Makerlink": "https://example.com/maker-7"}}]
    _fake_grist(monkeypatch, {MAKERS_URL: _response({"records": makers})})
    client = _configure(monkeypatch)

    assert await client._maker_link(["R", "GlazeMakers", 7]) == "https://example.com/maker-7"
