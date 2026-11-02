from bravo_findings.mask import mask, mask_text


def test_masks_account_access_key_and_ip():
    assert mask_text("acct 123456789012") == "acct ****9012"
    assert mask_text("key AKIAIOSFODNN7EXAMPLE") == "key AKIA****MPLE"
    assert mask_text("from 203.0.113.10") == "from 203.0.*.*"


def test_mask_is_idempotent():
    once = mask_text("123456789012 AKIAIOSFODNN7EXAMPLE 203.0.113.10")
    assert mask_text(once) == once


def test_mask_walks_nested_structures_and_keeps_other_types():
    raw = {"a": ["123456789012", {"b": 5, "c": True}], "d": None}
    assert mask(raw) == {"a": ["****9012", {"b": 5, "c": True}], "d": None}


def test_does_not_mask_short_numbers_or_timestamps():
    assert mask_text("port 8443, 2026-11-04T09:00:00Z") == "port 8443, 2026-11-04T09:00:00Z"
