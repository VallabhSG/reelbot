from typing import Any, Protocol

from ..series import Character


class MediaProvider(Protocol):
    provider_name: str

    def image(self, ctx: dict[str, Any], job_id: int | None) -> str: ...
    def video(self, ctx: dict[str, Any], job_id: int | None) -> str: ...
    def reference_urls(self, characters: list[Character]) -> list[str]: ...


class JSONChat(Protocol):
    def json_chat(self, system: str, user: str, temperature: float = 0.9) -> dict[str, Any]: ...
