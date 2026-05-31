"""Unit tests for the two pure functions in src/grist.py.

`_ref_id` extracts a row id from a Grist Reference cell value (bare int or the
tagged ["R", "<Table>", <rowId>] form). `glaze_entities` maps a For_print row's
fields into the eight %E1%..%E8% template placeholders, reading
settings.glaze_site_url at call time (so tests monkeypatch it first).
"""

import pytest

from src.grist import _ref_id, glaze_entities
from tests.conftest import make_glaze_fields


# ── _ref_id ──────────────────────────────────────────────────────────────────

def test_ref_id_bare_int_returned_as_is():
    assert _ref_id(99) == 99


def test_ref_id_tagged_reference_returns_row_id():
    assert _ref_id(["R", "Tbl", 99]) == 99


def test_ref_id_true_is_none_bool_checked_before_int():
    # bool is a subclass of int; the function must reject it BEFORE the int path.
    assert _ref_id(True) is None


def test_ref_id_false_is_none():
    assert _ref_id(False) is None


def test_ref_id_none_is_none():
    assert _ref_id(None) is None


def test_ref_id_short_list_is_none():
    # len < 3 -> cannot be a tagged reference
    assert _ref_id(["R", "Tbl"]) is None


def test_ref_id_wrong_tag_is_none():
    # first element != "R"
    assert _ref_id(["X", "Tbl", 5]) is None


def test_ref_id_string_is_none():
    assert _ref_id("5") is None


# ── glaze_entities ───────────────────────────────────────────────────────────

@pytest.fixture
def site(monkeypatch):
    """Pin glaze_site_url to a known base and return it."""
    from src.settings import settings
    monkeypatch.setattr(settings, "glaze_site_url", "https://site")
    return "https://site"


def test_glaze_entities_full_record(site):
    fields = make_glaze_fields(
        GeneralName="Лазурит Синий",
        Surface="Глянцевая",
        Foodgrade="Пищевая",
        Makerlink="https://example.com/maker",
        Type="Порошок",
        Description="Тестовое описание",
    )
    result = glaze_entities(fields)
    assert result == [
        "Синий",                              # %E1% rest
        "Лазурит",                            # %E2% first
        "https://site/glaze/Лазурит",         # %E3%
        "https://example.com/maker",          # %E4% Makerlink
        "https://site/glaze/icon/GLS",        # %E5% glossy
        "https://site/glaze/icon/FG",         # %E6% food-grade
        "Порошок",                            # %E7% Type
        "Тестовое описание",                  # %E8% Description
    ]


def test_glaze_entities_returns_eight_values(site):
    assert len(glaze_entities(make_glaze_fields())) == 8


def test_glaze_entities_general_name_split(site):
    # E1 is the tail words, E2 is the first word.
    result = glaze_entities(make_glaze_fields(GeneralName="Лазурит Синий"))
    assert result[0] == "Синий"
    assert result[1] == "Лазурит"


def test_glaze_entities_single_word_name_has_empty_rest(site):
    result = glaze_entities(make_glaze_fields(GeneralName="Лазурит"))
    assert result[0] == ""           # no tail words
    assert result[1] == "Лазурит"


def test_glaze_entities_general_name_none_no_index_error(site):
    result = glaze_entities(make_glaze_fields(GeneralName=None))
    assert result[0] == ""
    assert result[1] == ""
    # E3 still builds without raising / without a stray "None"
    assert result[2] == "https://site/glaze/"


def test_glaze_entities_general_name_empty_string(site):
    result = glaze_entities(make_glaze_fields(GeneralName=""))
    assert result[0] == ""
    assert result[1] == ""
    assert result[2] == "https://site/glaze/"


def test_glaze_entities_non_glossy_surface_yields_matt(site):
    result = glaze_entities(make_glaze_fields(Surface="Матовая"))
    assert result[4] == "https://site/glaze/icon/MATT"


def test_glaze_entities_glossy_surface_yields_gls(site):
    result = glaze_entities(make_glaze_fields(Surface="Глянцевая"))
    assert result[4] == "https://site/glaze/icon/GLS"


def test_glaze_entities_non_foodgrade_yields_nfg(site):
    result = glaze_entities(make_glaze_fields(Foodgrade="Непищевая"))
    assert result[5] == "https://site/glaze/icon/NFG"


def test_glaze_entities_foodgrade_yields_fg(site):
    result = glaze_entities(make_glaze_fields(Foodgrade="Пищевая"))
    assert result[5] == "https://site/glaze/icon/FG"


def test_glaze_entities_none_optional_fields_become_empty_strings(site):
    # Maker/Makerlink/Type/Description None must render as "" — never the literal "None".
    result = glaze_entities(
        make_glaze_fields(Maker=None, Makerlink=None, Type=None, Description=None)
    )
    assert result[3] == ""   # Makerlink
    assert result[6] == ""   # Type
    assert result[7] == ""   # Description
    assert "None" not in result


def test_glaze_entities_site_trailing_slash_no_double_slash(monkeypatch):
    from src.settings import settings
    monkeypatch.setattr(settings, "glaze_site_url", "https://site/")
    result = glaze_entities(make_glaze_fields(GeneralName="Лазурит Синий"))
    assert result[2] == "https://site/glaze/Лазурит"
    assert result[4] == "https://site/glaze/icon/GLS"
    assert result[5] == "https://site/glaze/icon/FG"
    assert "//glaze" not in result[2]
