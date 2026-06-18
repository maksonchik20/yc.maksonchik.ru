from decimal import Decimal, ROUND_HALF_UP

REFERRAL_COMMISSION_RATE = Decimal('0.20')


def calculate_commission(payment_amount):
    if payment_amount is None:
        return Decimal('0.00')
    amount = Decimal(payment_amount)
    return (amount * REFERRAL_COMMISSION_RATE).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def normalize_user_name(label):
    name = (label or '').strip()
    if not name:
        return ''
    lowered = name.lower()
    suffixes = (
        ' real',
        ' test',
        ' тест',
        ' подруга',
        ' (inf)',
    )
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if lowered.endswith(suffix):
                name = name[: -len(suffix)].strip()
                lowered = name.lower()
                changed = True
                break
    return name
