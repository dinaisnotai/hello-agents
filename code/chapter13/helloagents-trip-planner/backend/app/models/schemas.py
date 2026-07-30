"""Pydantic models for the constraint-aware trip planner."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

from ..constraints.schema import Constraint, ValidationResult
from ..services.city_name_service import normalize_city_name
from .observability import PlanningRunTrace
from .quality import ExperienceEvaluation, ExperienceIssue, RepairIteration


AvoidCategory = Literal[
    "park",
    "museum",
    "shopping",
    "temple",
    "amusement",
    "zoo",
    "natural",
    "historic",
]


class TripRequest(BaseModel):
    """Request for a personalized trip plan."""

    city: str = Field(..., description="Destination city", example="北京")
    start_date: str = Field(..., description="Start date, YYYY-MM-DD", example="2026-08-01")
    end_date: str = Field(..., description="End date, YYYY-MM-DD", example="2026-08-03")
    travel_days: int = Field(..., description="Trip length in days", ge=1, le=30, example=3)
    transportation: str = Field(..., description="Preferred transportation", example="公共交通")
    accommodation: str = Field(..., description="Accommodation preference", example="经济型酒店")
    preferences: List[str] = Field(default_factory=list, description="Interest tags")
    free_text_input: Optional[str] = Field(default="", description="Extra natural-language requirements")

    budget_limit: Optional[int] = Field(default=None, ge=0, description="Total budget limit in CNY")
    pace: str = Field(default="balanced", description="Trip pace: relaxed/balanced/packed")
    must_visit: List[str] = Field(default_factory=list, description="Must-visit attractions")
    avoid_categories: List[AvoidCategory] = Field(default_factory=list, description="Canonical POI categories to avoid")
    dietary_restrictions: List[str] = Field(default_factory=list, description="Dietary restrictions")
    max_daily_walk_km: Optional[float] = Field(default=None, ge=0, description="Max daily walking distance")
    hotel_area: Optional[str] = Field(default="", description="Preferred hotel area")

    # Optional structured intent fields. They are backward compatible with the
    # existing frontend and let the workflow distinguish explicit form values
    # from information inferred from free text.
    travelers: List[str] = Field(default_factory=list, description="Travel companions")
    energy_preference: Optional[Literal["low", "normal", "high"]] = Field(
        default=None, description="Physical energy preference"
    )
    daily_start_time: Optional[str] = Field(default=None, description="Daily start time, HH:MM")
    daily_end_time: Optional[str] = Field(default=None, description="Daily end time, HH:MM")
    arrival_time: Optional[str] = Field(
        default=None,
        description="Actual destination arrival time on the first day, HH:MM",
    )
    departure_time: Optional[str] = Field(
        default=None,
        description="Actual destination departure time on the last day, HH:MM",
    )
    hard_constraints: List[str] = Field(default_factory=list, description="Explicit hard constraints")
    soft_preferences: List[str] = Field(default_factory=list, description="Explicit soft preferences")
    first_visit: Optional[bool] = Field(
        default=None,
        description="Whether this is the traveler's first visit to the destination",
    )
    prefer_classic: Optional[bool] = Field(
        default=None,
        description="Prefer iconic attractions over niche or specialist venues",
    )

    @field_validator("city", mode="before")
    @classmethod
    def normalize_city(cls, value: object) -> str:
        return normalize_city_name(str(value or ""))

    @field_validator(
        "daily_start_time",
        "daily_end_time",
        "arrival_time",
        "departure_time",
    )
    @classmethod
    def validate_daily_time(cls, value: Optional[str]) -> Optional[str]:
        if value in (None, ""):
            return None
        datetime.strptime(value, "%H:%M")
        return value

    class Config:
        json_schema_extra = {
            "example": {
                "city": "北京",
                "start_date": "2026-08-01",
                "end_date": "2026-08-03",
                "travel_days": 3,
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "preferences": ["历史文化", "美食"],
                "free_text_input": "希望少走路，安排一个博物馆",
                "budget_limit": 2500,
                "pace": "balanced",
                "must_visit": ["故宫"],
                "avoid_categories": ["shopping"],
                "dietary_restrictions": ["不吃海鲜"],
                "max_daily_walk_km": 8,
                "hotel_area": "东城区",
            }
        }


class POISearchRequest(BaseModel):
    keywords: str = Field(..., description="Search keywords", example="故宫")
    city: str = Field(..., description="City", example="北京")
    citylimit: bool = Field(default=True, description="Limit search within city")


class RouteRequest(BaseModel):
    origin_address: str = Field(..., description="Origin address")
    destination_address: str = Field(..., description="Destination address")
    origin_city: Optional[str] = Field(default=None, description="Origin city")
    destination_city: Optional[str] = Field(default=None, description="Destination city")
    route_type: str = Field(default="walking", description="walking/driving/transit")


class Location(BaseModel):
    longitude: float = Field(..., description="Longitude")
    latitude: float = Field(..., description="Latitude")


class Attraction(BaseModel):
    name: str
    address: str = ""
    location: Location
    coordinates: List[float] = Field(
        default_factory=list,
        description="[latitude, longitude] planning coordinates",
    )
    visit_duration: int = Field(default=120, description="Visit duration in minutes")
    suggested_duration_minutes: Optional[int] = Field(
        default=None,
        ge=30,
        description="Structured POI dwell-time metadata before pace adjustment",
    )
    description: str = ""
    category: Optional[str] = Field(default="景点")
    categories: List[str] = Field(
        default_factory=list,
        description="Normalized interest categories used by deterministic scoring",
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Searchable interest tags used by attraction scoring",
    )
    area: str = Field(default="", description="Administrative or planning area")
    popularity: int = Field(default=5, ge=0, le=10)
    first_visit_priority: int = Field(default=5, ge=0, le=10)
    is_core_landmark: bool = Field(
        default=False,
        description="Protected city landmark for first-visit portfolio coverage",
    )
    crowd_level: int = Field(default=5, ge=0, le=10)
    intensity_level: Literal["low", "medium", "high"] = "medium"
    estimated_internal_walking_km: float = Field(default=0.5, ge=0)
    accessible: Optional[bool] = None
    rating: Optional[float] = None
    photos: Optional[List[str]] = Field(default_factory=list)
    poi_id: Optional[str] = ""
    parent_poi_id: str = Field(
        default="",
        description="Provider POI id of the containing attraction or venue",
    )
    visit_key: str = Field(
        default="",
        description="Canonical identity used to prevent duplicate visits",
    )
    image_url: Optional[str] = None
    ticket_price: int = Field(default=0, ge=0)
    score: float = Field(default=0, description="Internal planning score")
    recall_sources: List[str] = Field(
        default_factory=list,
        description="Auditable candidate-recall origins",
    )
    score_breakdown: Dict[str, float] = Field(
        default_factory=dict,
        description="Explainable components of the deterministic POI score",
    )
    opening_hours: str = ""
    opening_time: Optional[str] = None
    closing_time: Optional[str] = None
    latest_entry_time: Optional[str] = None
    hours_source: str = "unknown"
    planned_arrival_time: Optional[str] = None
    planned_departure_time: Optional[str] = None
    opening_hours_status: Literal["open", "closed", "unknown"] = "unknown"
    selection_role: Literal[
        "core_landmark",
        "major_attraction",
        "complementary_attraction",
        "niche_attraction",
    ] = "complementary_attraction"
    selection_reason: str = ""
    selection_trace: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Auditable candidate acceptance and rejection decisions",
    )


class Meal(BaseModel):
    type: str = Field(..., description="breakfast/lunch/dinner/snack")
    name: str
    address: Optional[str] = None
    location: Optional[Location] = None
    description: Optional[str] = None
    estimated_cost: int = Field(default=0, ge=0)


class Hotel(BaseModel):
    name: str
    address: str = ""
    location: Optional[Location] = None
    price_range: str = ""
    rating: str = ""
    distance: str = ""
    type: str = ""
    estimated_cost: int = Field(default=0, ge=0)
    selection_score: float = Field(
        default=0,
        description="Deterministic accommodation-selection score",
    )
    score_breakdown: Dict[str, float] = Field(default_factory=dict)


class RouteStep(BaseModel):
    mode: str = Field(description="walking/bus/subway/railway/driving")
    name: str = ""
    origin: str = ""
    destination: str = ""
    distance_meters: float = 0
    duration_minutes: int = 0
    instruction: str = ""


class RouteSegment(BaseModel):
    day_index: int
    origin: str
    destination: str
    route_type: str = "walking"
    distance_meters: float = 0
    duration_minutes: int = 0
    walking_distance_meters: float = 0
    walking_duration_minutes: int = 0
    transit_duration_minutes: int = 0
    steps: List[RouteStep] = Field(default_factory=list)
    description: str = ""
    planned_departure_time: Optional[str] = None
    planned_arrival_time: Optional[str] = None


class ScheduleBlock(BaseModel):
    type: Literal["rest", "meal", "buffer"] = "rest"
    start_time: str
    end_time: str
    reason: str = ""


class DayPlan(BaseModel):
    date: str
    day_index: int
    description: str
    transportation: str
    accommodation: str
    hotel: Optional[Hotel] = None
    attractions: List[Attraction] = Field(default_factory=list)
    meals: List[Meal] = Field(default_factory=list)
    route_segments: List[RouteSegment] = Field(default_factory=list)
    schedule_blocks: List[ScheduleBlock] = Field(default_factory=list)
    max_walking_leg_minutes: Optional[int] = Field(default=None, ge=0)
    daily_distance_km: float = 0
    daily_walking_distance_km: float = 0
    daily_visit_minutes: int = 0
    daily_travel_minutes: int = 0
    daily_buffer_minutes: int = 0
    daily_meal_minutes: int = Field(
        default=0,
        description="Meal time included in daily_buffer_minutes",
    )
    daily_duration_minutes: int = 0
    daily_elapsed_minutes: int = Field(
        default=0,
        description="Actual minutes from daily departure until return, excluding planning buffer",
    )
    day_utilization_score: float = Field(
        default=0,
        ge=0,
        description="Scheduled visit, route and meal time as a percentage of daily availability",
    )
    daily_cost: int = 0
    planned_start_time: Optional[str] = None
    planned_end_time: Optional[str] = None
    available_minutes: Optional[int] = Field(
        default=None,
        ge=0,
        description="Actual destination time available on this calendar day",
    )
    partial_day_reason: Optional[Literal["arrival", "departure"]] = None
    primary_plan: List[Attraction] = Field(
        default_factory=list,
        description="Primary attractions for the day; mirrors attractions in API output",
    )
    weather_backup: List[Attraction] = Field(
        default_factory=list,
        description="Indoor alternatives selected only from known candidates",
    )
    weather_warning: str = ""


class WeatherInfo(BaseModel):
    date: str
    day_weather: str = ""
    night_weather: str = ""
    day_temp: Union[int, str] = 0
    night_temp: Union[int, str] = 0
    wind_direction: str = ""
    wind_power: str = ""

    @field_validator("day_temp", "night_temp", mode="before")
    @classmethod
    def parse_temperature(cls, value):
        if isinstance(value, str):
            cleaned = value.replace("°C", "").replace("℃", "").replace("°", "").strip()
            try:
                return int(float(cleaned))
            except ValueError:
                return 0
        return value


class Budget(BaseModel):
    total_attractions: int = 0
    total_hotels: int = 0
    total_meals: int = 0
    total_transportation: int = 0
    total: int = 0
    budget_limit: Optional[int] = None
    remaining: Optional[int] = None


class ConstraintItem(BaseModel):
    name: str
    passed: bool
    actual: str = ""
    expected: str = ""
    severity: str = Field(default="info", description="info/warning/blocker")
    message: str = ""


class ConstraintReport(BaseModel):
    passed: bool = True
    score: float = Field(default=1.0, ge=0, le=1)
    items: List[ConstraintItem] = Field(default_factory=list)


class PlanReviewScores(BaseModel):
    """Explainable 0-100 soft quality scores for the generated route."""

    score: int = Field(default=100, ge=0, le=100)
    route_score: int = Field(default=100, ge=0, le=100)
    distance_score: int = Field(default=100, ge=0, le=100)
    time_score: int = Field(default=100, ge=0, le=100)
    experience_score: int = Field(default=100, ge=0, le=100)
    preference_score: int = Field(default=100, ge=0, le=100)
    diversity_score: int = Field(default=100, ge=0, le=100)
    budget_score: int = Field(default=100, ge=0, le=100)
    score_breakdown: Dict[str, float] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)


class EvidenceSource(BaseModel):
    title: str
    city: str = ""
    source: str = ""
    snippet: str
    score: float = 0


class PlanningTraceItem(BaseModel):
    """One observable decision made during constraint-driven planning."""

    iteration: int = Field(ge=0, description="Zero-based planning iteration")
    role: str = Field(description="The component that made the observation or decision")
    action: str = Field(description="Observed check or repair action")
    reason: str = Field(description="Why this action was taken")
    score_before: float = Field(ge=0, le=1, description="Constraint score before the action")
    score_after: float = Field(ge=0, le=1, description="Constraint score after the action")


class CandidateScoreDebug(BaseModel):
    """Top scored POI candidates retained for planning diagnostics."""

    name: str
    category: str = "general"
    tags: List[str] = Field(default_factory=list)
    score: float = 0
    score_breakdown: Dict[str, float] = Field(default_factory=dict)


class TripPlan(BaseModel):
    city: str
    start_date: str
    end_date: str
    days: List[DayPlan]
    weather_info: List[WeatherInfo] = Field(default_factory=list)
    overall_suggestions: str
    budget: Optional[Budget] = None
    route_segments: List[RouteSegment] = Field(default_factory=list)
    constraint_report: ConstraintReport = Field(default_factory=ConstraintReport)
    risk_warnings: List[str] = Field(default_factory=list)
    evidence_sources: List[EvidenceSource] = Field(default_factory=list)
    planning_trace: List[PlanningTraceItem] = Field(default_factory=list)
    candidate_debug: List[CandidateScoreDebug] = Field(default_factory=list)
    review_scores: PlanReviewScores = Field(default_factory=PlanReviewScores)
    normalized_constraints: List[Constraint] = Field(default_factory=list)
    validation_result: ValidationResult = Field(default_factory=ValidationResult)
    failure_reason: Optional[str] = None
    quality_gate_passed: bool = True
    quality_evaluation: Optional[ExperienceEvaluation] = None
    unresolved_quality_issues: List[ExperienceIssue] = Field(
        default_factory=list
    )
    unresolved_blocking_issues: List[ExperienceIssue] = Field(default_factory=list)
    unresolved_non_blocking_issues: List[ExperienceIssue] = Field(default_factory=list)
    degraded_reason: Optional[str] = None
    suggested_alternatives: List[str] = Field(default_factory=list)
    best_effort: bool = False
    repair_history: List[RepairIteration] = Field(default_factory=list)
    observability_trace: Optional[PlanningRunTrace] = None


class TripPlanResponse(BaseModel):
    success: bool
    message: str = ""
    data: Optional[TripPlan] = None
    session_id: Optional[str] = None
    plan_version: Optional[int] = None
    workflow: Optional["WorkflowExecutionSummary"] = None


class WorkflowExecutionSummary(BaseModel):
    """Optional execution metadata that does not change the trip-plan payload."""

    run_id: str
    workflow_mode: str
    degraded_services: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    resumed: bool = False


class ReplanRequest(BaseModel):
    plan: TripPlan
    request: Optional[TripRequest] = None
    notes: Optional[str] = ""


class POIInfo(BaseModel):
    id: str = ""
    parent_poi_id: str = ""
    visit_key: str = ""
    name: str
    type: str = "景点"
    address: str = ""
    location: Location
    coordinates: List[float] = Field(
        default_factory=list,
        description="[latitude, longitude] planning coordinates",
    )
    tel: Optional[str] = None
    rating: Optional[float] = None
    ticket_price: int = 0
    opening_hours: str = ""
    opening_time: Optional[str] = None
    closing_time: Optional[str] = None
    latest_entry_time: Optional[str] = None
    hours_source: str = "unknown"
    categories: List[str] = Field(default_factory=list)
    area: str = ""
    popularity: int = Field(default=5, ge=0, le=10)
    first_visit_priority: int = Field(default=5, ge=0, le=10)
    duration_minutes: Optional[int] = Field(default=None, ge=30)
    crowd_level: int = Field(default=5, ge=0, le=10)


class POISearchResponse(BaseModel):
    success: bool
    message: str = ""
    data: List[POIInfo] = Field(default_factory=list)


class RouteInfo(BaseModel):
    distance: float = Field(default=0, description="Distance in meters")
    duration: int = Field(default=0, description="Duration in seconds")
    route_type: str = "walking"
    walking_distance: float = Field(default=0, description="Walking distance in meters")
    walking_duration: int = Field(default=0, description="Walking duration in seconds")
    transit_duration: int = Field(default=0, description="Non-walking transit duration in seconds")
    steps: List[RouteStep] = Field(default_factory=list)
    description: str = ""


class RouteResponse(BaseModel):
    success: bool
    message: str = ""
    data: Optional[RouteInfo] = None


class WeatherResponse(BaseModel):
    success: bool
    message: str = ""
    data: List[WeatherInfo] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    success: bool = False
    message: str
    error_code: Optional[str] = None
