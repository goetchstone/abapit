import pytest

from abapit.blueprint_plan import plan_relationship

CATALOG = {"A1": "App One", "A2": "App Two", "A3": "App Three"}


def test_add_only_new_members():
    p = plan_relationship("add", ["A1", "A2"], ["A1"], CATALOG)
    assert [r["id"] for r in p["add"]] == ["A2"]
    assert [n["id"] for n in p["noops"]] == ["A1"]   # already attached
    assert p["changes"] == p["add"]


def test_remove_only_current_members():
    p = plan_relationship("remove", ["A1", "A3"], ["A1", "A2"], CATALOG)
    assert [r["id"] for r in p["remove"]] == ["A1"]
    assert [n["id"] for n in p["noops"]] == ["A3"]   # not attached
    assert p["changes"] == p["remove"]


def test_unknown_ids_flagged_and_labels_attached():
    p = plan_relationship("add", ["A1", "NOPE"], [], CATALOG)
    assert p["unknown"] == ["NOPE"]
    assert p["add"] == [{"id": "A1", "label": "App One"}]


def test_dedup_and_blank_ignored():
    p = plan_relationship("add", ["A1", "A1", "", "A2"], [], CATALOG)
    assert [r["id"] for r in p["add"]] == ["A1", "A2"]


def test_invalid_op_raises():
    with pytest.raises(ValueError):
        plan_relationship("nope", [], [], CATALOG)
