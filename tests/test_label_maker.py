"""Tests for src/label_maker.py: _load_template, lazy template caching, render_entities.

HTTP is faked with respx; no real Label Maker service is contacted.
"""

import json

import httpx
import pytest
import respx

from src.label_maker import LabelMaker, LabelMakerError

# ── _load_template (unit, filesystem via tmp_path) ───────────────────────────


def test_load_template_valid_json_returns_dict(tmp_path):
    path = tmp_path / "tpl.json"
    path.write_text(
        json.dumps({"nodes": [{"id": "a"}], "widthMm": 58, "heightMm": 40}),
        encoding="utf-8",
    )
    tpl = LabelMaker._load_template(str(path))
    assert isinstance(tpl, dict)
    assert tpl["widthMm"] == 58
    assert tpl["nodes"] == [{"id": "a"}]


def test_load_template_nodes_not_a_list_raises_labelmakererror(tmp_path):
    path = tmp_path / "tpl.json"
    # nodes present but a string, not a list -> rejected by the isinstance check
    path.write_text(json.dumps({"nodes": "x"}), encoding="utf-8")
    with pytest.raises(LabelMakerError, match="no nodes array"):
        LabelMaker._load_template(str(path))


def test_load_template_malformed_json_raises(tmp_path):
    path = tmp_path / "tpl.json"
    path.write_text("{not valid json", encoding="utf-8")
    # The code does not wrap json.loads errors, so a JSONDecodeError propagates.
    with pytest.raises(Exception) as exc_info:
        LabelMaker._load_template(str(path))
    assert isinstance(exc_info.value, json.JSONDecodeError)


def test_load_template_nonexistent_path_raises_labelmakererror(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(LabelMakerError, match="Cannot read template"):
        LabelMaker._load_template(str(missing))


# ── lazy template caching ────────────────────────────────────────────────────


def test_lazy_construction_does_not_load_template(monkeypatch):
    calls = []

    def fake_load(path):
        calls.append(path)
        return {"nodes": []}

    monkeypatch.setattr(LabelMaker, "_load_template", staticmethod(fake_load))
    LabelMaker("some/path.json", lazy=True)
    # Construction with lazy=True must not touch the template.
    assert calls == []


def test_lazy_template_property_loads_once_and_caches(monkeypatch):
    calls = []

    def fake_load(path):
        calls.append(path)
        return {"nodes": [], "loaded": True}

    monkeypatch.setattr(LabelMaker, "_load_template", staticmethod(fake_load))
    lm = LabelMaker("some/path.json", lazy=True)

    first = lm.template
    second = lm.template
    assert first == {"nodes": [], "loaded": True}
    assert first is second  # cached identity, not re-built
    assert calls == ["some/path.json"]  # loaded exactly once


# ── render_entities (integration, respx) ─────────────────────────────────────

GEN_URL = "http://lm.test/api/generate-pdf"


def _lazy_maker(monkeypatch):
    """A LabelMaker whose template is injected (no 222KB file read) and whose
    base_url points at the faked host."""
    from src.settings import settings
    monkeypatch.setattr(settings, "label_maker_url", "http://lm.test")
    lm = LabelMaker(lazy=True)
    lm.base_url = "http://lm.test"  # __init__ rstrips the setting; pin it explicitly
    lm._template = {"nodes": []}
    return lm


@respx.mock
async def test_render_entities_returns_pdf_bytes_on_200(monkeypatch):
    lm = _lazy_maker(monkeypatch)
    pdf = b"%PDF-1.4 some real pdf bytes"
    route = respx.post(GEN_URL).mock(
        return_value=httpx.Response(200, content=pdf)
    )

    result = await lm.render_entities(["hello"])

    assert result == pdf
    assert route.called
    # the template + rows entities are forwarded in the JSON body
    sent = json.loads(route.calls.last.request.content)
    assert sent["rows"] == [{"entities": ["hello"]}]
    assert sent["template"] == {"nodes": []}


@respx.mock
async def test_render_entities_non_200_raises_with_status_and_detail(monkeypatch):
    lm = _lazy_maker(monkeypatch)
    respx.post(GEN_URL).mock(
        return_value=httpx.Response(500, text="boom upstream failure")
    )

    with pytest.raises(LabelMakerError) as exc_info:
        await lm.render_entities(["x"])

    msg = str(exc_info.value)
    assert "500" in msg
    assert "boom upstream failure" in msg


@respx.mock
async def test_render_entities_non_pdf_content_raises(monkeypatch):
    lm = _lazy_maker(monkeypatch)
    respx.post(GEN_URL).mock(
        return_value=httpx.Response(200, content=b"not a pdf")
    )

    with pytest.raises(LabelMakerError, match="did not return a PDF"):
        await lm.render_entities(["x"])


@respx.mock
async def test_render_entities_transport_error_raises_labelmakererror(monkeypatch):
    lm = _lazy_maker(monkeypatch)
    respx.post(GEN_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(LabelMakerError, match="unreachable"):
        await lm.render_entities(["x"])
