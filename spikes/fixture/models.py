"""User records and lookups."""

from dataclasses import dataclass


@dataclass
class User:
    id: int
    email: str
    is_active: bool


_USERS = {
    1: User(id=1, email="ada@example.com", is_active=True),
    2: User(id=2, email="grace@example.com", is_active=False),
}


def get_user(user_id):
    """Return the user, or None when there is no such user."""
    return _USERS.get(user_id)


def list_active_users():
    """Return every user whose account is still active."""
    return [user for user in _USERS.values() if user.is_active]
