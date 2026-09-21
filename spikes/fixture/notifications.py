"""Outbound notifications, backed by a third-party client."""

from external_client import Client

client = Client()


def notify(user_id):
    user = client.get_user(user_id)
    return client.send(user.email, "hello")
