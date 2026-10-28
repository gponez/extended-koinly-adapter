"""Exclude borrowing activity from the core release.

Interest history is collected only to detect unsupported activity. Empty payment
history is not proof that no principal borrowing exists; no loan, repayment or
interest transaction is inferred or exported.
"""
from .core import ValidationError


def require_no_interest(data, account_id, *, source_parameters):
    """Require a correctly scoped, empty individual-payment response."""
    if (not isinstance(source_parameters, dict)
            or set(source_parameters) != {'accountId', 'interval'}
            or source_parameters.get('interval') != 'ALL'
            or isinstance(source_parameters.get('accountId'), bool)
            or not isinstance(source_parameters.get('accountId'), (str, int))
            or str(source_parameters['accountId']) != str(account_id)):
        raise ValidationError('Interest exclusion requires an unfiltered single-account interval=ALL request')
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        raise ValidationError('Interest exclusion requires individual data.items, not chart aggregates')
    if data['items']:
        raise ValidationError('Interest/borrowing activity is unsupported in the core release; no CSV was generated')
    return {'status': 'NO_PAYMENT_RECORDS', 'payment_count': 0,
            'purpose': 'Exclusion guard only; borrowing, repayment and interest export are unsupported.'}
