"""Display audited integer-cent amounts without floating-point conversion."""


def yuan(cents: int) -> str:
    """Format cents exactly, retaining significant decimals and no exponent."""
    if type(cents) is not int:
        raise TypeError('金额请使用整数分')
    whole, fraction = divmod(abs(cents), 100)
    text = str(whole)
    if fraction:
        text += '.' + f'{fraction:02d}'.rstrip('0')
    return ('-' if cents < 0 else '') + text
