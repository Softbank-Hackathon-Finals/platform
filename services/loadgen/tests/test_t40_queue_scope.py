"""Disposable T40 queue qualification; never target develop."""
from pathlib import Path


def test_prior_service_is_compatible_with_later_change():
    marker = Path(__file__).resolve().parents[3] / "scripts/t40_scope_marker.txt"
    assert not marker.exists(), "T40_EXPECTED_COMBINATION_FAILURE"
