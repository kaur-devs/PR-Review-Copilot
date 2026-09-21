"""Reporting, which imports the lookup under a different name."""

from models import get_user as fetch_user


def report_line(user_id):
    user = fetch_user(user_id)
    return f"{user.id},{user.email}"
