"""Application-facing user operations."""

from models import get_user


def user_email(user_id):
    user = get_user(user_id)
    return user.email


def is_active(user_id):
    user = get_user(user_id)
    return user.is_active
