"""Pydantic models for the constraint-aware trip planner."""

from __future__ import annotations

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


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
    visit_duration: int = Field(default=120, description="Visit duration in minutes")
    description: str = ""
    category: Optional[str] = Field(default="景点")
    rating: Optional[float] = None
    photos: Optional[List[str]] = Field(default_factory=list)
    poi_id: Optional[str] = ""
    image_url: Optional[str] = None
    ticket_price: int = Field(default=0, ge=0)
    score: float = Field(default=0, description="Internal planning score")


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
    daily_distance_km: float = 0
    daily_walking_distance_km: float = 0
    daily_visit_minutes: int = 0
    daily_travel_minutes: int = 0
    daily_buffer_minutes: int = 0
    daily_duration_minutes: int = 0
    daily_cost: int = 0


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
    failure_reason: Optional[str] = None


class TripPlanResponse(BaseModel):
    success: bool
    message: str = ""
    data: Optional[TripPlan] = None


class ReplanRequest(BaseModel):
    plan: TripPlan
    request: Optional[TripRequest] = None
    notes: Optional[str] = ""


class POIInfo(BaseModel):
    id: str = ""
    name: str
    type: str = "景点"
    address: str = ""
    location: Location
    tel: Optional[str] = None
    rating: Optional[float] = None
    ticket_price: int = 0


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
