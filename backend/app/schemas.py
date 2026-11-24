from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class StopDraft(BaseModel):
    location_id: str
    items: list[str] = Field(default_factory=list)
    priority: Literal["normal", "urgent"] = "normal"


class TaskDraft(BaseModel):
    mode: Literal["real", "sim"] = "sim"
    pickup_location_id: str
    stops: list[StopDraft] = Field(default_factory=list)
    deadline: str | None = None
    deadline_kind: Literal["none", "clock"] = "none"


class ParseRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    clarification: dict | None = None


class ParseResponse(BaseModel):
    draft: TaskDraft
    missing_fields: list[str]
    notes: list[str]
    parser: str
    raw_text: str


class CreateTaskRequest(BaseModel):
    mode: Literal["real", "sim"] = "sim"
    scene_id: str | None = None
    pickup_location_id: str
    stops: list[StopDraft]
    deadline: str | None = None
    deadline_kind: Literal["none", "clock"] = "none"
    raw_input: str = ""
    parser: str = "manual"
    confirmed: bool = True


class PlanRequest(BaseModel):
    start_time: str | None = None


class AcceptTaskRequest(BaseModel):
    assignee: str = Field(min_length=1, max_length=32)
    note: str = ""


class TaskNoteRequest(BaseModel):
    author: str = Field(default="管理员", max_length=32)
    content: str = Field(min_length=1, max_length=500)


class ActionRequest(BaseModel):
    action: Literal[
        "start",
        "pause",
        "resume",
        "arrive",
        "confirm_pickup",
        "confirm_delivery",
        "complete",
        "cancel",
    ]
    request_id: str = Field(min_length=8, max_length=64)
    plan_version: int = 0
    stop_id: str | None = None
    note: str = ""


class ReplanRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=64)
    reason: str = ""
    plan_version: int = 0


class RoadEventRequest(BaseModel):
    edge_id: str
    edge_fraction: float | None = Field(default=None, ge=0, le=1)
    status: Literal["open", "congested", "blocked"]
    scene_id: str = "sim_default"
    authority: Literal["normal", "hard"] = "normal"
    source: Literal["user_text", "user_photo", "model_candidate", "admin"] = "user_text"
    confirmed_by: str = Field(default="user", max_length=64)
    note: str = ""
    expires_at: str | None = None


class SimTickRequest(BaseModel):
    seconds: float = Field(default=1.0, gt=0, le=3600)
    speed_mps: float | None = Field(default=None, gt=0, le=1000)


class SeedRequest(BaseModel):
    reset: bool = False


class SemanticEventRequest(BaseModel):
    """Already interpreted upstream output. Fractions run from edge.from to edge.to."""

    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=8, max_length=100)
    task_id: str = Field(min_length=1, max_length=64)
    obstacle_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1)
    map_version: str
    source: Literal["mock_vision_llm", "vision_llm"] = "mock_vision_llm"
    observed_at: AwareDatetime | None = None
    lifecycle: Literal["observed", "cleared"] = "observed"
    edge_id: str
    edge_fraction: float | None = Field(default=None, ge=0, le=1)
    category: Literal["construction", "pedestrian", "crowd", "box", "barrier", "other"] = "other"
    passability: Literal["open", "congested", "blocked", "unknown"]
    confidence: float = Field(default=1, ge=0, le=1)
    expected_duration_s: float | None = Field(default=None, ge=0, le=86400)
    summary: str = Field(min_length=1, max_length=1000)


class SimRuntimeRequest(BaseModel):
    enabled: bool
    speed_mps: float = Field(default=40, gt=0, le=1000)
    auto_upstream: bool = False
    upstream_category: Literal["pedestrian", "construction", "crowd", "box", "barrier"] = "pedestrian"
