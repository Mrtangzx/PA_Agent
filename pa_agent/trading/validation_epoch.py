"""Validation epochs bind every promotion fact to one exact private pool.

The registry is the single seam used by collectors, exporters, backtests,
promotion and the workbench.  A metadata refresh keeps the same epoch; an
add/remove revision creates a new one and preserves every older epoch for
audit.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from pa_agent.trading.hotspots import HOTSPOT_RULE_VERSION
from pa_agent.trading.topdown import TOPDOWN_SCORING_VERSION, TOPDOWN_STRATEGY_ID
from pa_agent.trading.universe import (
    CLOUD_AI_AUTHORIZATION_SYMBOLS,
    CLOUD_AI_STRATEGY_FROZEN_AT,
    CLOUD_AI_SYMBOLS,
    CLOUD_AI_UNIVERSE_ID,
    PRIVATE_A_SHARE_UNIVERSE_ID,
    UniverseSnapshot,
    cloud_ai_definition_hash,
)

VALIDATION_EPOCH_SCHEMA = "validation_epoch_v1"
EPOCH_OBSERVATION_PREFIX = "validation_epoch_observations_v1"


class ValidationEpoch(BaseModel):
    """Immutable validation identity plus refresh-only pool aliases."""

    schema_version: str = VALIDATION_EPOCH_SCHEMA
    epoch_id: str
    strategy_version: str = TOPDOWN_STRATEGY_ID
    universe_id: str
    pool_version: str
    origin_pool_version: str
    pool_versions: list[str] = Field(min_length=1)
    member_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbols: list[str]
    authorization_symbols: list[str]
    activated_at: str
    scoring_version: str = TOPDOWN_SCORING_VERSION
    hotspot_rule_version: str = HOTSPOT_RULE_VERSION
    status: str = "collecting"

    @property
    def observation_strategy_version(self) -> str:
        if self.status == "legacy_audit" and self.universe_id == CLOUD_AI_UNIVERSE_ID:
            return self.strategy_version
        return f"{EPOCH_OBSERVATION_PREFIX}:{self.epoch_id}"

    @property
    def is_private_pool(self) -> bool:
        return self.universe_id == PRIVATE_A_SHARE_UNIVERSE_ID

    @model_validator(mode="after")
    def validate_definition(self) -> ValidationEpoch:
        if len(self.symbols) != len(set(self.symbols)):
            raise ValueError("validation_epoch_symbols_must_be_unique")
        if not set(self.authorization_symbols).issubset(self.symbols):
            raise ValueError("validation_epoch_authorization_symbols_not_subset")
        if self.pool_version not in self.pool_versions:
            raise ValueError("validation_epoch_current_pool_version_missing")
        if self.origin_pool_version != self.pool_versions[0]:
            raise ValueError("validation_epoch_origin_pool_version_mismatch")
        point = datetime.fromisoformat(self.activated_at)
        if point.tzinfo is None:
            raise ValueError("validation_epoch_activated_at_requires_timezone")
        return self


class ValidationEpochRegistry:
    """Create or locate the one epoch that owns current validation evidence."""

    def __init__(self, store: Any, *, clock=None) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now().astimezone())

    def activate(
        self,
        universe_snapshot: UniverseSnapshot | dict[str, Any],
        *,
        activated_at: datetime | None = None,
    ) -> ValidationEpoch:
        snapshot = UniverseSnapshot.model_validate(universe_snapshot)
        member_hash = _snapshot_member_hash(snapshot)
        current = self.current(create_default=False)
        if current is not None and current.member_hash == member_hash:
            if snapshot.version in current.pool_versions:
                return current
            refreshed = current.model_copy(update={
                "pool_version": snapshot.version,
                "pool_versions": [*current.pool_versions, snapshot.version],
            })
            self.store.upsert_validation_epoch(refreshed, make_current=True)
            return refreshed

        point = (activated_at or self.clock()).astimezone()
        authorization = [
            item.symbol
            for item in snapshot.members
            if item.symbol in snapshot.symbols and item.authorization_eligible
        ]
        if not snapshot.members:
            authorization = list(snapshot.symbols)
        epoch_id = _epoch_id(snapshot.version, member_hash, point)
        epoch = ValidationEpoch(
            epoch_id=epoch_id,
            universe_id=(
                PRIVATE_A_SHARE_UNIVERSE_ID
                if snapshot.source_kind == "user_managed_a_share_universe"
                else CLOUD_AI_UNIVERSE_ID
            ),
            pool_version=snapshot.version,
            origin_pool_version=snapshot.version,
            pool_versions=[snapshot.version],
            member_hash=member_hash,
            symbols=list(snapshot.symbols),
            authorization_symbols=authorization,
            activated_at=point.isoformat(),
        )
        self.store.upsert_validation_epoch(epoch, make_current=True)
        return epoch

    def current(self, *, create_default: bool = True) -> ValidationEpoch | None:
        row = self.store.current_validation_epoch()
        if row is not None:
            return ValidationEpoch.model_validate(row["epoch"])
        if not create_default:
            return None
        universes = self.store.list_universe_snapshots(limit=1)
        if universes:
            return self.activate(universes[0]["snapshot"])
        return self._activate_legacy_seed()

    def require_current(self) -> ValidationEpoch:
        epoch = self.current()
        if epoch is None:  # defensive; current() creates the safe seed
            raise ValueError("validation_epoch_missing")
        return epoch

    def bind_hotspot(
        self,
        snapshot: Any,
        *,
        expected_epoch_id: str = "",
        expected_pool_version: str = "",
    ):
        """Stamp a freshly captured pool hotspot with its owning epoch.

        ``expected_*`` identifies the pool that launched an asynchronous
        hotspot request.  A response that arrives after a member revision is
        still useful as general risk news, but it must not be relabelled as
        evidence for the new validation epoch.
        """
        from pa_agent.trading.topdown import HotspotSnapshot

        value = HotspotSnapshot.model_validate(snapshot)
        epoch = self.require_current()
        captured = datetime.fromisoformat(value.captured_at)
        frozen = datetime.fromisoformat(value.frozen_at)
        activated = datetime.fromisoformat(epoch.activated_at)
        if captured.tzinfo is None or frozen.tzinfo is None or activated.tzinfo is None:
            raise ValueError("hotspot_epoch_binding_requires_timezone")
        request_mismatch = (
            bool(expected_epoch_id and expected_epoch_id != epoch.epoch_id)
            or bool(
                expected_pool_version
                and expected_pool_version not in epoch.pool_versions
            )
        )
        time_invalid = (
            frozen < activated
            or captured < frozen
            or captured < activated
        )
        if value.symbol not in epoch.symbols or request_mismatch or time_invalid:
            gaps = list(value.data_gaps)
            if request_mismatch:
                gaps.append("hotspot_request_validation_epoch_mismatch")
            if time_invalid:
                gaps.append("hotspot_validation_epoch_time_invalid")
            return value.model_copy(update={
                "validation_epoch_id": "",
                "pool_version": "",
                "member_hash": "",
                "data_gaps": list(dict.fromkeys(gaps)),
            }).with_source_hash()
        return value.model_copy(update={
            "validation_epoch_id": epoch.epoch_id,
            "pool_version": epoch.pool_version,
            "member_hash": epoch.member_hash,
        }).with_source_hash()

    def _activate_legacy_seed(self) -> ValidationEpoch:
        activated = datetime.fromisoformat(CLOUD_AI_STRATEGY_FROZEN_AT)
        epoch = ValidationEpoch(
            epoch_id="legacy-cloud-ai-11-v1",
            universe_id=CLOUD_AI_UNIVERSE_ID,
            pool_version=f"{CLOUD_AI_UNIVERSE_ID}-2026-08",
            origin_pool_version=f"{CLOUD_AI_UNIVERSE_ID}-2026-08",
            pool_versions=[f"{CLOUD_AI_UNIVERSE_ID}-2026-08"],
            member_hash=cloud_ai_definition_hash(),
            symbols=list(CLOUD_AI_SYMBOLS),
            authorization_symbols=list(CLOUD_AI_AUTHORIZATION_SYMBOLS),
            activated_at=activated.isoformat(),
            status="legacy_audit",
        )
        self.store.upsert_validation_epoch(epoch, make_current=True)
        return epoch


def _snapshot_member_hash(snapshot: UniverseSnapshot) -> str:
    declared = str(snapshot.member_hash or snapshot.source_hash or "").strip()
    if len(declared) == 64 and all(value in "0123456789abcdef" for value in declared):
        return declared
    raw = json.dumps(
        [str(symbol).strip() for symbol in snapshot.symbols],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _epoch_id(pool_version: str, member_hash: str, activated_at: datetime) -> str:
    raw = f"{pool_version}|{member_hash}|{activated_at.isoformat()}"
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"ve-{activated_at:%Y%m%d-%H%M%S%f}-{member_hash[:12]}-{suffix}"
