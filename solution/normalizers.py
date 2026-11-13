"""Pure data normalizers and parsers for phone, date, decimal, enum, and timestamps."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

from solution.config import IST, UTC

# English 3-letter month abbreviations mapped to month numbers (1-12)
ENGLISH_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def normalize_string(raw: Optional[str]) -> Optional[str]:
    """Trim string. An empty or whitespace-only value counts as missing (None)."""
    if raw is None:
        return None
    val = raw.strip()
    return val if val else None


def normalize_phone(raw: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Validate and normalize Indian mobile phone numbers per specification:
    
    1. Strip every non-digit.
    2. If 12 digits starting with '91', drop the '91'.
    3. If 11 digits starting with '0', drop the '0'.
    4. Valid only if exactly 10 digits and starts with 6, 7, 8 or 9.
    5. Store canonically as +91XXXXXXXXXX.
    
    Returns:
        (is_valid, canonical_phone_or_none)
    """
    if raw is None:
        return False, None
    raw_str = raw.strip()
    if not raw_str:
        return False, None

    digits = re.sub(r"\D", "", raw_str)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in "6789":
        return True, f"+91{digits}"
    return False, None


def normalize_date(raw: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Parse and normalize dates per specification:
    
    Accepts:
    - YYYY-MM-DD
    - DD/MM/YYYY (day-first)
    - DD-MM-YYYY (day-first)
    - DD-Mon-YYYY (day-first, e.g. 01-Dec-2026, month is case-insensitive)
    
    Impossible dates (like 31/02) are invalid.
    Returns:
        (is_valid, iso_date_str_or_none)  e.g. (True, "2026-11-05")
    """
    if raw is None:
        return False, None
    s = raw.strip()
    if not s:
        return False, None

    parsed_date: Optional[date] = None

    # 1. YYYY-MM-DD
    m1 = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m1:
        try:
            parsed_date = date(int(m1.group(1)), int(m1.group(2)), int(m1.group(3)))
        except ValueError:
            return False, None

    # 2. DD/MM/YYYY (day-first)
    if not parsed_date:
        m2 = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
        if m2:
            try:
                parsed_date = date(int(m2.group(3)), int(m2.group(2)), int(m2.group(1)))
            except ValueError:
                return False, None

    # 3. DD-MM-YYYY (day-first)
    if not parsed_date:
        m3 = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{4})$", s)
        if m3:
            try:
                parsed_date = date(int(m3.group(3)), int(m3.group(2)), int(m3.group(1)))
            except ValueError:
                return False, None

    # 4. DD-Mon-YYYY (day-first, month case-insensitive)
    if not parsed_date:
        m4 = re.match(r"^(\d{1,2})-([a-zA-Z]{3})-(\d{4})$", s)
        if m4:
            mon_str = m4.group(2).lower()
            mon_num = ENGLISH_MONTHS.get(mon_str)
            if mon_num:
                try:
                    parsed_date = date(int(m4.group(3)), mon_num, int(m4.group(1)))
                except ValueError:
                    return False, None

    if parsed_date is not None:
        return True, parsed_date.strftime("%Y-%m-%d")
    return False, None


def normalize_decimal(raw: Optional[object]) -> Tuple[bool, Optional[float]]:
    """Normalize decimal currency values per specification:
    
    1. Optionally prefixed by '₹', 'Rs', 'Rs.', or 'INR' (case-insensitive, optional space).
    2. Commas are thousands separators.
    3. Must be >= 0.
    4. Round to 2 decimal places.
    
    Returns:
        (is_valid, rounded_float_or_none)
    """
    if raw is None:
        return False, None
    s = str(raw).strip()
    if not s:
        return False, None

    # Match optional currency prefix followed by positive numbers and thousands separators
    pattern = r"^(?:₹|rs\.|rs|inr)?\s*([0-9,]+(?:\.[0-9]+)?)$"
    match = re.match(pattern, s, re.IGNORECASE)
    if not match:
        return False, None

    clean_num_str = match.group(1).replace(",", "")
    try:
        val = float(clean_num_str)
        if val < 0:
            return False, None
        return True, round(val, 2)
    except ValueError:
        return False, None


def normalize_enum(raw: Optional[str], allowed_values: List[str]) -> Tuple[bool, Optional[str]]:
    """Normalize enum values per specification:
    
    Compared case-insensitively against values and stored lower-case.
    
    Returns:
        (is_valid, lowercase_enum_or_none)
    """
    if raw is None:
        return False, None
    val = raw.strip().lower()
    if not val:
        return False, None

    allowed_lower = {v.lower(): v.lower() for v in allowed_values}
    if val in allowed_lower:
        return True, allowed_lower[val]
    return False, None


def parse_timestamp(raw_ts: str) -> Tuple[datetime, datetime, str, str, str]:
    """Parse timestamps per specification:
    
    Timestamps come in three forms:
    1. UTC ('...Z')
    2. Offset ('...+05:30')
    3. Naive ('2026-10-03 10:01:00') -> Naive means IST (+05:30)
    
    Returns:
        (dt_utc, dt_ist, utc_iso_str, ist_iso_str, ist_date_str)
        
    Example:
        dt_utc: aware UTC datetime
        dt_ist: aware IST datetime (+05:30)
        utc_iso_str: '2026-10-01T04:30:00Z'
        ist_iso_str: '2026-10-01T10:00:00+05:30'
        ist_date_str: '2026-10-01'
    """
    s = raw_ts.strip()
    if not s:
        raise ValueError("Timestamp string cannot be empty")

    if s.endswith("Z"):
        # Format: 2026-10-01T05:26:10Z or with microseconds
        dt_base = datetime.fromisoformat(s[:-1])
        dt = dt_base.replace(tzinfo=UTC)
    elif ("+" in s[10:]) or ("-" in s[10:]):
        # Format with timezone offset: 2026-10-01T10:40:19+05:30
        dt = datetime.fromisoformat(s)
    else:
        # Naive datetime: represents IST per specification
        dt_naive = datetime.fromisoformat(s)
        dt = dt_naive.replace(tzinfo=IST)

    dt_utc = dt.astimezone(UTC)
    dt_ist = dt.astimezone(IST)

    # UTC ISO-8601 format without microsecond jitter if 0
    if dt_utc.microsecond:
        utc_iso_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    else:
        utc_iso_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    ist_iso_str = dt_ist.isoformat()
    ist_date_str = dt_ist.strftime("%Y-%m-%d")

    return dt_utc, dt_ist, utc_iso_str, ist_iso_str, ist_date_str
