from decimal import Decimal, ROUND_HALF_UP


def normalize_zero_money(value):
    """
    Normalize monetary value to eliminate negative zero.
    If value is approximately zero (rounds to 0.00 at 2 decimal places),
    or is already zero/negative zero, return Decimal("0.00").
    """
    if value is None:
        return Decimal("0.00")
    try:
        dec = Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError):
        return Decimal("0.00")

    quantized = dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if quantized == Decimal("0.00") or abs(quantized) == Decimal("0.00"):
        return Decimal("0.00")
    return dec


def format_money(value, currency="৳"):
    """
    Format a monetary value to 2 decimal places with the specified currency symbol.
    Consistently normalizes zero monetary values (including negative zero such as
    -0.00, -0.0, or small negative values rounding to zero) to '৳0.00' (never '-৳0.00').
    Negative values are formatted as '-৳X.XX'.
    """
    if value is None:
        return f"{currency}0.00"
    try:
        dec = Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError):
        return f"{currency}0.00"

    quantized = dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if quantized == Decimal("0.00") or abs(quantized) == Decimal("0.00"):
        return f"{currency}0.00"

    if quantized < Decimal("0.00"):
        return f"-{currency}{abs(quantized):.2f}"

    return f"{currency}{quantized:.2f}"
