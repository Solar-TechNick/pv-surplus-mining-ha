import pytest

from custom_components.pv_surplus_mining.normalize import normalize_grid_power


@pytest.mark.parametrize("raw", [None, "unknown", "unavailable", "", "n/a", "NaNx"])
def test_invalid_returns_none(raw):
    assert normalize_grid_power(raw, True) is None


def test_import_positive_passthrough():
    assert normalize_grid_power("1500", True) == 1500.0


def test_export_positive_meter_is_negated():
    # meter reports +export; internal convention is +import, so flip
    assert normalize_grid_power("1500", False) == -1500.0
    assert normalize_grid_power(-800, True) == -800.0
