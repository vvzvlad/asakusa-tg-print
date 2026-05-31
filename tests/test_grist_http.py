"""HTTP-level tests for src.grist.GristClient using respx-mocked httpx.

GristClient reads settings in __init__, so every test monkeypatches the
src.settings.settings singleton BEFORE constructing the client. The two endpoints
exercised are:
    GET https://grist.test/api/docs/DOC/tables/For_print/records
    GET https://grist.test/api/docs/DOC/tables/GlazeMakers/records
"""

import httpx
import pytest
import respx

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
    monkeypatch.setattr(settings, "label_maker_timeout", 5)
    return GristClient()


# ── _records ─────────────────────────────────────────────────────────────────

@respx.mock
async def test_records_without_api_key_raises_before_any_http(monkeypatch):
    """Empty GRIST_API_KEY must raise GristError without issuing any request."""
    route = respx.get(FOR_PRINT_URL).mock(return_value=httpx.Response(200, json={"records": []}))
    client = _configure(monkeypatch, api_key="")

    with pytest.raises(GristError, match="GRIST_API_KEY is not configured"):
        await client._records(GLAZE_TABLE)

    assert not route.called


@respx.mock
async def test_records_non_200_raises_with_body_text(monkeypatch):
    """A 500 response surfaces the status and (truncated) body in the error."""
    respx.get(FOR_PRINT_URL).mock(return_value=httpx.Response(500, text="boom internal error"))
    client = _configure(monkeypatch)

    with pytest.raises(GristError) as exc:
        await client._records(GLAZE_TABLE)

    assert "500" in str(exc.value)
    assert "boom internal error" in str(exc.value)


@respx.mock
async def test_records_transport_error_raises_gristerror(monkeypatch):
    """A connection-level failure is wrapped as GristError, not leaked as httpx error."""
    respx.get(FOR_PRINT_URL).mock(side_effect=httpx.ConnectError("refused"))
    client = _configure(monkeypatch)

    with pytest.raises(GristError, match="Grist unreachable"):
        await client._records(GLAZE_TABLE)


@respx.mock
async def test_records_missing_records_key_raises(monkeypatch):
    """A 200 whose JSON lacks the 'records' key is treated as an unexpected response."""
    respx.get(FOR_PRINT_URL).mock(return_value=httpx.Response(200, json={"oops": True}))
    client = _configure(monkeypatch)

    with pytest.raises(GristError, match="Unexpected Grist response"):
        await client._records(GLAZE_TABLE)


@respx.mock
async def test_records_returns_records_list(monkeypatch):
    """A well-formed 200 returns the list under the 'records' key verbatim."""
    records = [make_glaze_record(record_id=1), make_glaze_record(record_id=2, GeneralName="Other")]
    respx.get(FOR_PRINT_URL).mock(return_value=httpx.Response(200, json={"records": records}))
    client = _configure(monkeypatch)

    result = await client._records(GLAZE_TABLE)

    assert result == records


@respx.mock
async def test_records_sends_bearer_authorization_header(monkeypatch):
    """The API key is sent as a Bearer Authorization header."""
    route = respx.get(FOR_PRINT_URL).mock(return_value=httpx.Response(200, json={"records": []}))
    client = _configure(monkeypatch)

    await client._records(GLAZE_TABLE)

    assert route.called
    assert route.calls.last.request.headers["Authorization"] == "Bearer KEY"


# ── find_glaze ───────────────────────────────────────────────────────────────

@respx.mock
async def test_find_glaze_exact_match_preferred_over_substring(monkeypatch):
    """An exact case-insensitive match wins even when a substring candidate is earlier."""
    records = [
        # substring candidate appears FIRST in table order
        make_glaze_record(record_id=1, GeneralName="Лазурит Синий Глубокий", Maker=None),
        # exact (modulo case) candidate appears LATER
        make_glaze_record(record_id=2, GeneralName="лазурит синий", Maker=None),
    ]
    respx.get(FOR_PRINT_URL).mock(return_value=httpx.Response(200, json={"records": records}))
    client = _configure(monkeypatch)

    fields = await client.find_glaze("Лазурит Синий")

    assert fields is not None
    assert fields["GeneralName"] == "лазурит синий"


@respx.mock
async def test_find_glaze_substring_fallback_when_no_exact(monkeypatch):
    """With no exact match, the first case-insensitive substring match is returned."""
    records = [
        make_glaze_record(record_id=1, GeneralName="Изумруд Зелёный", Maker=None),
        make_glaze_record(record_id=2, GeneralName="Лазурит Синий Глубокий", Maker=None),
    ]
    respx.get(FOR_PRINT_URL).mock(return_value=httpx.Response(200, json={"records": records}))
    client = _configure(monkeypatch)

    fields = await client.find_glaze("лазурит")

    assert fields is not None
    assert fields["GeneralName"] == "Лазурит Синий Глубокий"


@respx.mock
async def test_find_glaze_no_match_returns_none(monkeypatch):
    """A query that matches nothing returns None."""
    records = [make_glaze_record(record_id=1, GeneralName="Изумруд Зелёный", Maker=None)]
    respx.get(FOR_PRINT_URL).mock(return_value=httpx.Response(200, json={"records": records}))
    client = _configure(monkeypatch)

    assert await client.find_glaze("нет такого") is None


@respx.mock
async def test_find_glaze_trims_query_whitespace(monkeypatch):
    """Leading/trailing whitespace in the query is trimmed before matching."""
    records = [make_glaze_record(record_id=1, GeneralName="Лазурит Синий", Maker=None)]
    respx.get(FOR_PRINT_URL).mock(return_value=httpx.Response(200, json={"records": records}))
    client = _configure(monkeypatch)

    fields = await client.find_glaze("   Лазурит Синий   ")

    assert fields is not None
    assert fields["GeneralName"] == "Лазурит Синий"


@respx.mock
async def test_find_glaze_resolves_maker_link_from_makers_table(monkeypatch):
    """The matched row's Maker ref is resolved into a Makerlink via GlazeMakers."""
    for_print = [make_glaze_record(record_id=1, GeneralName="Лазурит Синий", Maker=7)]
    makers = [
        {"id": 3, "fields": {"Makerlink": "https://example.com/wrong"}},
        {"id": 7, "fields": {"Makerlink": "https://example.com/maker-7"}},
    ]
    respx.get(FOR_PRINT_URL).mock(return_value=httpx.Response(200, json={"records": for_print}))
    respx.get(MAKERS_URL).mock(return_value=httpx.Response(200, json={"records": makers}))
    client = _configure(monkeypatch)

    fields = await client.find_glaze("Лазурит Синий")

    assert fields is not None
    assert fields["Makerlink"] == "https://example.com/maker-7"


# ── _maker_link ──────────────────────────────────────────────────────────────

@respx.mock
async def test_maker_link_none_ref_returns_empty_without_http(monkeypatch):
    """A missing/None Maker ref short-circuits to "" and does not hit GlazeMakers."""
    route = respx.get(MAKERS_URL).mock(return_value=httpx.Response(200, json={"records": []}))
    client = _configure(monkeypatch)

    assert await client._maker_link(None) == ""
    assert not route.called


@respx.mock
async def test_maker_link_unknown_id_returns_empty(monkeypatch):
    """A Maker id absent from GlazeMakers resolves to an empty string."""
    makers = [{"id": 1, "fields": {"Makerlink": "https://example.com/a"}}]
    respx.get(MAKERS_URL).mock(return_value=httpx.Response(200, json={"records": makers}))
    client = _configure(monkeypatch)

    assert await client._maker_link(999) == ""


@respx.mock
async def test_maker_link_tagged_reference_form_resolves(monkeypatch):
    """The tagged ['R', '<Table>', <rowId>] reference form is resolved too."""
    makers = [{"id": 7, "fields": {"Makerlink": "https://example.com/maker-7"}}]
    respx.get(MAKERS_URL).mock(return_value=httpx.Response(200, json={"records": makers}))
    client = _configure(monkeypatch)

    assert await client._maker_link(["R", "GlazeMakers", 7]) == "https://example.com/maker-7"
