"""Exact arithmetic helpers.

All money / rate values are handled as :class:`fractions.Fraction` so that
"按原值核对" and the precise conversion tables never go through binary float.

Input values are taken from their *raw text* form (API fields are strings),
which avoids the precision loss of a JSON number being decoded to ``float``
before validation.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any


class ExactParseError(ValueError):
    """Raised when a decimal / fraction text cannot be parsed exactly."""


_FRACTION_RE = re.compile(r"^\s*([+-]?\d+)\s*/\s*(\d+)\s*$")
_INTEGER_RE = re.compile(r"^\s*[+-]?\d+\s*$")


def parse_decimal_text(value: Any, *, field: str = "value") -> Fraction:
    """Parse a finite decimal text into an exact Fraction.

    Only decimal syntax is accepted (no ``a/b`` fraction), because monetary
    amounts are required to be decimal positive numbers.  The raw string is
    fed to :class:`decimal.Decimal` and then converted losslessly.
    """

    if not isinstance(value, str):
        raise ExactParseError(f"{field} 必须是十进制字符串（避免浮点误差），收到 {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ExactParseError(f"{field} 不能为空")
    # Reject fraction syntax explicitly for money fields.
    if "/" in text:
        raise ExactParseError(f"{field} 金额必须是十进制数，不能是分数: {value!r}")
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ExactParseError(f"{field} 不是合法的十进制数: {value!r}") from None
    if not dec.is_finite():
        raise ExactParseError(f"{field} 必须是有限十进制数: {value!r}")
    return Fraction(dec)


def parse_positive_decimal(value: Any, *, field: str = "金额") -> Fraction:
    fr = parse_decimal_text(value, field=field)
    if fr <= 0:
        raise ExactParseError(f"{field} 必须为正数: {value!r}")
    return fr


def parse_rational_text(value: Any, *, field: str = "数值") -> Fraction:
    """Parse an endpoint that may be a decimal (``7.15``) or a fraction (``143/20``)."""

    if not isinstance(value, str):
        raise ExactParseError(f"{field} 必须是字符串（十进制或正分数），收到 {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ExactParseError(f"{field} 不能为空")

    m = _FRACTION_RE.match(text)
    if m:
        num_s, den_s = m.groups()
        num, den = int(num_s), int(den_s)
        if den == 0:
            raise ExactParseError(f"{field} 分母不能为 0: {value!r}")
        fr = Fraction(num, den)
    else:
        # Accept plain integers / decimal / scientific decimal via Decimal.
        if not (_INTEGER_RE.match(text) or _looks_decimal(text)):
            raise ExactParseError(f"{field} 不是合法的十进制数或分数 a/b: {value!r}")
        try:
            dec = Decimal(text)
        except (InvalidOperation, ValueError):
            raise ExactParseError(f"{field} 不是合法的十进制数或分数: {value!r}") from None
        if not dec.is_finite():
            raise ExactParseError(f"{field} 必须有限: {value!r}")
        fr = Fraction(dec)

    if fr <= 0:
        raise ExactParseError(f"{field} 必须为正分数: {value!r}")
    return fr


def _looks_decimal(text: str) -> bool:
    try:
        Decimal(text)
        return True
    except (InvalidOperation, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Serialization helpers (Fraction <-> JSON-safe int pairs / strings)
# --------------------------------------------------------------------------- #


def frac_to_pair(fr: Fraction) -> list[int]:
    return [fr.numerator, fr.denominator]


def frac_from_pair(pair: Any) -> Fraction:
    if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
        raise ValueError(f"非法分数表示: {pair!r}")
    n, d = pair
    return Fraction(int(n), int(d))


def frac_to_text(fr: Fraction) -> str:
    """Canonical exact text: integer when integral, otherwise ``n/d``."""
    if fr.denominator == 1:
        return str(fr.numerator)
    return f"{fr.numerator}/{fr.denominator}"


def frac_to_decimal_approx(fr: Fraction, places: int = 12) -> str:
    """Human-readable *approximate* decimal for display only.

    The authoritative representation stays :func:`frac_to_text` (exact n/d).
    """
    sign = "-" if fr < 0 else ""
    fr = abs(fr)
    whole, rem = divmod(fr.numerator, fr.denominator)
    digits = []
    for _ in range(places):
        rem *= 10
        digits.append(str(rem // fr.denominator))
        rem %= fr.denominator
        if rem == 0:
            break
    frac_part = "".join(digits)
    if frac_part:
        return f"{sign}{whole}.{frac_part}"
    return f"{sign}{whole}"
