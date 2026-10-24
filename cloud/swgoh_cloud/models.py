from datetime import datetime, timezone, timedelta
import json
import re
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OPERATIONS = {
    "PlayerRpc": {"Ping", "GetInitialData", "UpdateInbox", "BulkClaimDailyReward", "ClaimDailyReward", "ClaimAllEnergyRewards", "ClaimEnergyReward"},
    "MatchMakingRpc": {"FindPlayer"},
    "GuildRpc": {"GetGuild", "GetLeaderboard", "GetExchangeDonationRequests", "GetPlayerTributes"},
    "StoreRpc": {"RefreshShipments", "BuyShipmentItem", "BuyItem"},
    "BattleRpc": {"BattleStart", "BattleFinish"},
    "ChannelRpc": {"GetEvents"},
    "Inspector": {"Snapshot"},
}
TOPICS = {"shipments", "inbox", "guild", "energy"}
SECRET_KEYS = {"authorization", "cookie", "setcookie", "password", "identitytoken", "accesstoken", "refreshtoken", "sessiontoken", "requesttoken", "apikey", "inspectorKey".lower(), "headers", "rawdata", "rawbody", "requestenvelope", "serializedpayload", "claimreference"}


def check_tree(value: Any, depth=0, budget=None):
    if budget is None:
        budget = [20000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 16:
        raise ValueError("Projection exceeds structural limits")
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in SECRET_KEYS or normalized.endswith("token") or len(key) > 160:
                raise ValueError("Credential or raw transport field is forbidden")
            check_tree(item, depth + 1, budget)
    elif isinstance(value, list):
        if len(value) > 5000:
            raise ValueError("Projection list exceeds limit")
        for item in value:
            check_tree(item, depth + 1, budget)
    elif isinstance(value, str) and len(value) > 4096:
        raise ValueError("Projection string exceeds limit")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountCreate(StrictModel):
    alias: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")


class SourceCreate(StrictModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")


class Observation(StrictModel):
    session: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    event_key: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_:.-]+$")
    revision: int = Field(ge=1, le=1000000)
    observed_at: datetime
    service: str = Field(max_length=64)
    operation: str = Field(max_length=80)
    topic: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] | None = None

    @field_validator("observed_at")
    @classmethod
    def timestamp(cls, value):
        if value.tzinfo is None or value > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("Observation needs a timezone and cannot be in the future")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def supported(self):
        if self.operation not in OPERATIONS.get(self.service, set()):
            raise ValueError("RPC operation is not in the observation allowlist")
        if self.topic is not None and self.topic not in TOPICS:
            raise ValueError("Unsupported snapshot topic")
        if self.service == "Inspector" and (self.topic is None or self.payload is None):
            raise ValueError("Inspector snapshots require a topic and payload")
        if len(json.dumps(self.metadata, allow_nan=False).encode()) > 16384:
            raise ValueError("Observation metadata exceeds 16 KiB")
        for value in (self.metadata, self.payload):
            check_tree(value)
        if len(json.dumps(self.model_dump(mode="json"), allow_nan=False).encode()) > 1024 * 1024:
            raise ValueError("Observation exceeds one MiB")
        return self


class IngestBatch(StrictModel):
    events: list[Observation] = Field(min_length=1, max_length=50)
