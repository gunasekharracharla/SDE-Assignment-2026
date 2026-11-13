"""Unit tests for phone, date, decimal, enum, string, and timestamp normalizers."""

from datetime import datetime, timezone
import pytest

from solution.config import IST, UTC
from solution.normalizers import (
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_phone,
    normalize_string,
    parse_timestamp,
)


# --- Phone Tests ---
def test_normalize_phone_valid():
    """Verify various valid Indian mobile representations canonicalize to +91XXXXXXXXXX."""
    valid_cases = [
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("91-98765-43210", "+919876543210"),
        ("(+91) 9876543210", "+919876543210"),
        ("+919876543210", "+919876543210"),
        ("6123456789", "+916123456789"),  # Starts with 6
        ("7123456789", "+917123456789"),  # Starts with 7
        ("8123456789", "+918123456789"),  # Starts with 8
        ("9123456789", "+919123456789"),  # Starts with 9
    ]
    for raw, expected in valid_cases:
        ok, res = normalize_phone(raw)
        assert ok is True, f"Failed for {raw}"
        assert res == expected


def test_normalize_phone_invalid():
    """Verify invalid phone numbers are rejected."""
    invalid_cases = [
        "5123456789",       # Starts with 5 (not 6,7,8,9)
        "1234567890",       # Starts with 1
        "987654321",        # 9 digits
        "98765432100",      # 11 digits without leading 0
        "9198765432100",    # 13 digits
        "022-2456-7890",    # Landline
        "+1 415 555 0134",  # US phone
        "N/A",              # Text
        "",                 # Empty
        "   ",              # Whitespace
        None,               # None
    ]
    for raw in invalid_cases:
        ok, res = normalize_phone(raw)
        assert ok is False, f"Expected {raw} to fail"
        assert res is None


# --- Date Tests ---
def test_normalize_date_valid():
    """Verify acceptance of all 4 specified date formats (day-first)."""
    valid_cases = [
        ("2026-10-05", "2026-10-05"),     # YYYY-MM-DD
        ("05/11/2026", "2026-11-05"),     # DD/MM/YYYY
        ("14-10-2026", "2026-10-14"),     # DD-MM-YYYY
        ("01-Dec-2026", "2026-12-01"),    # DD-Mon-YYYY mixed case
        ("21-nov-2026", "2026-11-21"),    # DD-Mon-YYYY lowercase
        ("15-AUG-2026", "2026-08-15"),    # DD-Mon-YYYY uppercase
    ]
    for raw, expected in valid_cases:
        ok, res = normalize_date(raw)
        assert ok is True, f"Failed for {raw}"
        assert res == expected


def test_normalize_date_invalid():
    """Verify impossible dates and malformed strings are rejected."""
    invalid_cases = [
        "31/02/2026",       # Impossible day
        "29-Feb-2026",      # 2026 is not a leap year
        "31-Apr-2026",      # April has 30 days
        "2026-13-01",       # Impossible month
        "soon",
        "TBD",
        "2026/10/05",       # Wrong delimiter
        "",
        None,
    ]
    for raw in invalid_cases:
        ok, res = normalize_date(raw)
        assert ok is False, f"Expected {raw} to fail"
        assert res is None


# --- Decimal Tests ---
def test_normalize_decimal_valid():
    """Verify currency prefixes, thousands separators, rounding to 2 places."""
    valid_cases = [
        ("₹27,554", 27554.0),
        ("Rs. 24787", 24787.0),
        ("INR 25301.4", 25301.4),
        ("6850", 6850.0),
        ("4737.14", 4737.14),
        (" ₹ 1,234.567 ", 1234.57),      # Rounding to 2 places
        ("Rs 500.5", 500.5),
        (12000, 12000.0),
        (450.75, 450.75),
    ]
    for raw, expected in valid_cases:
        ok, res = normalize_decimal(raw)
        assert ok is True, f"Failed for {raw}"
        assert res == expected


def test_normalize_decimal_invalid():
    """Verify negative amounts and non-numeric values are rejected."""
    invalid_cases = [
        "-100",
        "-₹50",
        "N/A",
        "-",
        "twelve thousand",
        "",
        None,
    ]
    for raw in invalid_cases:
        ok, res = normalize_decimal(raw)
        assert ok is False, f"Expected {raw} to fail"
        assert res is None


# --- Enum Tests ---
def test_normalize_enum_valid():
    """Verify case-insensitive enum matching with lowercase output."""
    allowed = ["en", "hi", "ta", "kn", "te", "mr"]
    assert normalize_enum("en", allowed) == (True, "en")
    assert normalize_enum("HI", allowed) == (True, "hi")
    assert normalize_enum(" Ta ", allowed) == (True, "ta")


def test_normalize_enum_invalid():
    """Verify unknown enums and invalid values are rejected."""
    allowed = ["en", "hi", "ta"]
    assert normalize_enum("Hindi", allowed) == (False, None)
    assert normalize_enum("english", allowed) == (False, None)
    assert normalize_enum("", allowed) == (False, None)
    assert normalize_enum(None, allowed) == (False, None)


# --- String Tests ---
def test_normalize_string():
    """Verify string trimming and empty string converted to None."""
    assert normalize_string("  Aarav Menon  ") == "Aarav Menon"
    assert normalize_string("") is None
    assert normalize_string("   ") is None
    assert normalize_string(None) is None


# --- Timestamp Tests ---
def test_parse_timestamp_three_forms():
    """Verify UTC, offset, and naive forms resolve to the identical UTC instant."""
    ts_utc = "2026-10-01T05:26:10Z"
    ts_offset = "2026-10-01T10:56:10+05:30"
    ts_naive = "2026-10-01 10:56:10"  # Naive means IST

    dt_u1, dt_i1, u_str1, i_str1, d_str1 = parse_timestamp(ts_utc)
    dt_u2, dt_i2, u_str2, i_str2, d_str2 = parse_timestamp(ts_offset)
    dt_u3, dt_i3, u_str3, i_str3, d_str3 = parse_timestamp(ts_naive)

    # All three must represent the exact same chronological instant
    assert dt_u1 == dt_u2 == dt_u3
    assert dt_i1 == dt_i2 == dt_i3
    assert u_str1 == u_str2 == u_str3 == "2026-10-01T05:26:10Z"
    assert i_str1 == i_str2 == i_str3 == "2026-10-01T10:56:10+05:30"
    assert d_str1 == d_str2 == d_str3 == "2026-10-01"


def test_parse_timestamp_date_boundary():
    """Verify UTC timestamp crossing midnight in IST reflects the correct IST date."""
    # 20:00 UTC on Oct 1st is 01:30 AM on Oct 2nd in IST (+5:30)
    ts_utc = "2026-10-01T20:00:00Z"
    dt_u, dt_i, u_str, i_str, d_str = parse_timestamp(ts_utc)

    assert d_str == "2026-10-02"
    assert i_str == "2026-10-02T01:30:00+05:30"
    assert u_str == "2026-10-01T20:00:00Z"
