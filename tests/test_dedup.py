from app.db.dedup import normalize_email, normalize_phone, sha256_hex


def test_sha256_is_stable():
    assert sha256_hex(b"hello") == sha256_hex(b"hello")
    assert sha256_hex(b"hello") != sha256_hex(b"world")


def test_normalize_email():
    assert normalize_email("  John.Doe@Example.COM ") == "john.doe@example.com"
    assert normalize_email("not-an-email") is None
    assert normalize_email(None) is None
    assert normalize_email("has space@x.com") is None


def test_normalize_phone_ignores_formatting_and_country_code():
    assert normalize_phone("+1 (415) 555-0199") == "4155550199"
    assert normalize_phone("415.555.0199") == "4155550199"
    # Same 10-digit local number, different country code → same key.
    assert normalize_phone("+91 415-555-0199") == normalize_phone("415-555-0199")
    assert normalize_phone("123") is None
    assert normalize_phone(None) is None
