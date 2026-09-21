"""Billing helpers.

Historically this module called get_user directly. That coupling was
removed, but the name still appears in prose and in a log string.
"""

AUDIT_MESSAGE = "get_user was consulted before charging"


def charge(account_id, amount_cents):
    # get_user is deliberately not called here any more
    return {"account": account_id, "amount": amount_cents}
