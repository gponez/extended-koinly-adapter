"""Shared numeric, timestamp and Koinly CSV helpers for API conversion."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

D = Decimal
ZERO = D(0)
KOINLY_COLUMNS = ['Date', 'Sent Amount', 'Sent Currency', 'Received Amount',
                 'Received Currency', 'Tag', 'Description', 'TxHash']


class ValidationError(ValueError):
    pass


def dec(value):
    try:
        number = D(value)
    except (InvalidOperation, TypeError):
        raise ValidationError(f'Invalid decimal: {value!r}') from None
    if not number.is_finite():
        raise ValidationError('Non-finite numeric input')
    return number


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        raise ValidationError(f'Invalid ISO timestamp: {value!r}') from None
    if result.tzinfo is None:
        raise ValidationError('Timestamps must have an explicit timezone')
    return result.astimezone(timezone.utc)


def koinly_date(value):
    """Format validated UTC seconds without the per-row locale formatter."""
    result = timestamp(value)
    if result.year < 1000:
        # strftime's historical-year padding differs across platforms.
        return result.strftime('%Y-%m-%d %H:%M:%S')
    return result.replace(tzinfo=None).isoformat(sep=' ', timespec='seconds')


def fmt(value):
    return format(value, 'f')


def koinly_row(event):
    amount = dec(event['amount'])
    incoming = amount > 0
    return {'Date': koinly_date(event['time']),
            'Sent Amount': '' if incoming else fmt(-amount),
            'Sent Currency': '' if incoming else event['currency'],
            'Received Amount': fmt(amount) if incoming else '',
            'Received Currency': event['currency'] if incoming else '',
            'Tag': event['tag'], 'Description': f"Extended: {event['description']}",
            'TxHash': event['txhash']}
