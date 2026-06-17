from abc import ABC, abstractmethod
from typing import Protocol

from followup.agents.risk.schemas import RiskScoreSnapshot


class RiskSnapshotStore(Protocol):
    async def get_latest_snapshot(
        self,
        opportunity_id: str,
        workspace_id: str,
    ) -> RiskScoreSnapshot | None:
        ...

    async def save_snapshot(
        self,
        snapshot: RiskScoreSnapshot,
    ) -> RiskScoreSnapshot:
        ...


class BaseRiskSnapshotStore(ABC):
    @abstractmethod
    async def get_latest_snapshot(
        self,
        opportunity_id: str,
        workspace_id: str,
    ) -> RiskScoreSnapshot | None:
        raise NotImplementedError

    @abstractmethod
    async def save_snapshot(
        self,
        snapshot: RiskScoreSnapshot,
    ) -> RiskScoreSnapshot:
        raise NotImplementedError


class InMemoryRiskSnapshotStore(BaseRiskSnapshotStore):
    def __init__(self) -> None:
        self._snapshots: dict[tuple[str, str], list[RiskScoreSnapshot]] = {}

    async def get_latest_snapshot(
        self,
        opportunity_id: str,
        workspace_id: str,
    ) -> RiskScoreSnapshot | None:
        key = (workspace_id, opportunity_id)
        snapshots = self._snapshots.get(key, [])
        if not snapshots:
            return None
        return max(snapshots, key=lambda snapshot: snapshot.computed_at)

    async def save_snapshot(
        self,
        snapshot: RiskScoreSnapshot,
    ) -> RiskScoreSnapshot:
        key = (snapshot.workspace_id, snapshot.opportunity_id)
        self._snapshots.setdefault(key, []).append(snapshot)
        return snapshot

    def clear(self) -> None:
        self._snapshots.clear()
