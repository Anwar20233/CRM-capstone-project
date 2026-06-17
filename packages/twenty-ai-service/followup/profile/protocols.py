from typing import Any, Protocol
from uuid import UUID


class ProfileServiceProtocol(Protocol):
    async def apply_fact_updates(
        self,
        profile_id: UUID,
        candidates: list[dict[str, Any]],
        trigger: str,
        trigger_reference_id: str,
    ) -> dict[str, Any]:
        ...
