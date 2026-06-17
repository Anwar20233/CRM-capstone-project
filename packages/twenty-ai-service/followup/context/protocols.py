from typing import Awaitable, Protocol

from followup.context.schemas import DealContext


class DealContextExtractor(Protocol):
    def __call__(
        self,
        opportunity_id: str,
        workspace_id: str,
        user_id: str,
    ) -> Awaitable[DealContext]: ...
