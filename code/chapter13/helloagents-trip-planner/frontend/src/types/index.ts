export interface Location {
  longitude: number
  latitude: number
}

export interface Attraction {
  name: string
  address: string
  location: Location
  coordinates?: number[]
  visit_duration: number
  suggested_duration_minutes?: number
  description: string
  category?: string
  categories?: string[]
  tags?: string[]
  area?: string
  popularity?: number
  first_visit_priority?: number
  crowd_level?: number
  intensity_level?: 'low' | 'medium' | 'high'
  estimated_internal_walking_km?: number
  accessible?: boolean
  rating?: number
  photos?: string[]
  poi_id?: string
  image_url?: string
  ticket_price?: number
  score?: number
  recall_sources?: string[]
  score_breakdown?: Record<string, number>
  opening_hours?: string
  opening_time?: string
  closing_time?: string
  planned_arrival_time?: string
  planned_departure_time?: string
  opening_hours_status?: 'open' | 'closed' | 'unknown'
  hours_source?: string
}

export interface Meal {
  type: 'breakfast' | 'lunch' | 'dinner' | 'snack'
  name: string
  address?: string
  location?: Location
  description?: string
  estimated_cost?: number
}

export interface Hotel {
  name: string
  address: string
  location?: Location
  price_range: string
  rating: string
  distance: string
  type: string
  estimated_cost?: number
}

export interface RouteStep {
  mode: string
  name: string
  origin: string
  destination: string
  distance_meters: number
  duration_minutes: number
  instruction: string
}

export interface RouteSegment {
  day_index: number
  origin: string
  destination: string
  route_type: string
  distance_meters: number
  duration_minutes: number
  walking_distance_meters: number
  walking_duration_minutes: number
  transit_duration_minutes: number
  steps: RouteStep[]
  description: string
  planned_departure_time?: string
  planned_arrival_time?: string
}

export interface Budget {
  total_attractions: number
  total_hotels: number
  total_meals: number
  total_transportation: number
  total: number
  budget_limit?: number
  remaining?: number
}

export interface ConstraintItem {
  name: string
  passed: boolean
  actual: string
  expected: string
  severity: 'info' | 'warning' | 'blocker'
  message: string
}

export interface ConstraintReport {
  passed: boolean
  score: number
  items: ConstraintItem[]
}

export interface EvidenceSource {
  title: string
  city: string
  source: string
  snippet: string
  score: number
}

export interface PlanningTraceItem {
  iteration: number
  role: string
  action: string
  reason: string
  score_before: number
  score_after: number
}

export interface DayPlan {
  date: string
  day_index: number
  description: string
  transportation: string
  accommodation: string
  hotel?: Hotel
  attractions: Attraction[]
  meals: Meal[]
  route_segments?: RouteSegment[]
  schedule_blocks?: Array<{
    type: 'rest' | 'meal' | 'buffer'
    start_time: string
    end_time: string
    reason: string
  }>
  max_walking_leg_minutes?: number
  daily_distance_km?: number
  daily_walking_distance_km?: number
  daily_visit_minutes?: number
  daily_travel_minutes?: number
  daily_buffer_minutes?: number
  daily_meal_minutes?: number
  daily_duration_minutes?: number
  daily_elapsed_minutes?: number
  day_utilization_score?: number
  daily_cost?: number
  planned_start_time?: string
  planned_end_time?: string
}

export interface WeatherInfo {
  date: string
  day_weather: string
  night_weather: string
  day_temp: number
  night_temp: number
  wind_direction: string
  wind_power: string
}

export interface TripPlan {
  city: string
  start_date: string
  end_date: string
  days: DayPlan[]
  weather_info: WeatherInfo[]
  overall_suggestions: string
  budget?: Budget
  route_segments: RouteSegment[]
  constraint_report: ConstraintReport
  risk_warnings: string[]
  evidence_sources: EvidenceSource[]
  planning_trace: PlanningTraceItem[]
  candidate_debug: Array<{
    name: string
    category: string
    tags: string[]
    score: number
    score_breakdown: Record<string, number>
  }>
  review_scores: {
    score: number
    route_score: number
    distance_score: number
    time_score: number
    experience_score: number
    preference_score: number
    diversity_score: number
    budget_score: number
    score_breakdown: Record<string, number>
    warnings: string[]
  }
  normalized_constraints: Array<{
    type: string
    operator: string
    value?: unknown
    unit: string
    start?: string
    end?: string
    reason: string
    severity: 'hard' | 'soft'
    source: string
  }>
  validation_result: {
    valid: boolean
    violations: Array<{
      type: string
      constraint_type: string
      day?: number
      message: string
      actual?: unknown
      expected?: unknown
      severity: 'hard' | 'soft'
      repair_hint: string
      poi_name?: string
    }>
    checked_constraints: number
    score: number
  }
  failure_reason?: string
}

export type AvoidCategory =
  | 'park'
  | 'museum'
  | 'shopping'
  | 'temple'
  | 'amusement'
  | 'zoo'
  | 'natural'
  | 'historic'

export interface TripFormData {
  city: string
  start_date: string
  end_date: string
  travel_days: number
  transportation: string
  accommodation: string
  preferences: string[]
  free_text_input: string
  budget_limit?: number
  pace: 'relaxed' | 'balanced' | 'packed'
  must_visit: string[]
  avoid_categories: AvoidCategory[]
  dietary_restrictions: string[]
  max_daily_walk_km?: number
  hotel_area?: string
  daily_start_time?: string
  daily_end_time?: string
  first_visit?: boolean
  prefer_classic?: boolean
}

export interface TripPlanResponse {
  success: boolean
  message: string
  data?: TripPlan
  session_id?: string
  plan_version?: number
}

export interface ReplanRequest {
  plan: TripPlan
  request?: TripFormData
  notes?: string
}

export interface TripRequestPatch {
  budget_limit?: number
  pace?: 'relaxed' | 'balanced' | 'packed'
  add_must_visit: string[]
  remove_must_visit: string[]
}

export interface ConversationMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  created_at: string
}

export interface PlanVersionSummary {
  version: number
  change_summary: string
  created_at: string
}

export interface TripSessionSummary {
  id: string
  title: string
  city: string
  current_version: number
  created_at: string
  updated_at: string
}

export interface TripSessionDetail extends TripSessionSummary {
  request: TripFormData
  plan: TripPlan
  messages: ConversationMessage[]
  versions: PlanVersionSummary[]
}

export interface TripSessionResponse {
  success: boolean
  message: string
  data: TripSessionDetail
}

export interface ChatMessageResponse {
  success: boolean
  assistant_message: string
  applied_patch: TripRequestPatch
  data: TripSessionDetail
}
