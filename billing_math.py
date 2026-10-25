"""Shared rupee calculations. Existing bills keep their stored line totals."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def number(value, field='amount'):
    try:
        result = Decimal(str(value if value not in (None, '') else 0))
    except (InvalidOperation, ValueError):
        raise ValueError(f'{field} must be a valid number')
    if not result.is_finite() or result < 0 or result > Decimal('1000000000'):
        raise ValueError(f'{field} must be between 0 and 1,000,000,000')
    return result


def money(value):
    return number(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def totals(bill, items, wallet_used=0, paid=False):
    subtotal = sum((money(i['total']) for i in items), Decimal(0))
    discount_value = number(bill.get('discount_value'))
    discount = money(subtotal * discount_value / 100 if bill.get('discount_type') == '%' else discount_value)
    surcharge = money(bill.get('surcharge_amount'))
    if not surcharge and bill.get('express_service'):
        value = number(bill.get('surcharge_value'))
        surcharge = money(subtotal * value / 100 if bill.get('surcharge_type') == '%' else value)
    sale = max(Decimal(0), subtotal - discount + surcharge)
    wallet = min(money(wallet_used), sale)
    payable = sale - wallet
    advance = money(bill.get('advance_paid'))
    balance = max(Decimal(0), payable - advance)
    cancelled = bool(bill.get('void'))
    return {k: float(v) for k, v in {
        'subtotal': subtotal, 'discount_amount': discount, 'surcharge_amount': surcharge,
        'total_before_wallet': sale, 'wallet_used': wallet, 'final_payable': payable,
        'advance_paid': advance, 'balance_amount': balance,
        'amount_due': Decimal(0) if paid or cancelled else balance,
    }.items()}
