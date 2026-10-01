"""Cliente HTTP del núcleo (users, channels, messages). Stub: paso 3."""


def get_channel(channel_id: str) -> dict:
    raise NotImplementedError


def get_user(user_id: str) -> dict:
    raise NotImplementedError


def list_messages(channel_id: str) -> list[dict]:
    raise NotImplementedError
