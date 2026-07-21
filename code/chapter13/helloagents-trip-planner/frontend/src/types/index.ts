export interface Location {
  longitude: number
  latitude: number
}

export interface Attraction {
  name: string
  address: string
  location: Location
  visit_duration: number
  description: string
  category?: string
  rating?: number
  photos?: string[]
  poi_id?: string
  image_url?: string
  ticket_price?: number
  score?: number
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

export interface RouteSegment {
  day_index: number
  origin: string
  destination: string
  route_type: string
  distance_meters: number
  duration_minutes: number
  description: string
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
  daily_distance_km?: number
  daily_cost?: number
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
}

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
  avoid_categories: string[]
  dietary_restrictions: string[]
  max_daily_walk_km?: number
  hotel_area?: string
}

export interface TripPlanResponse {
  success: boolean
  message: string
  data?: TripPlan
}

export interface ReplanRequest {
  plan: TripPlan
  request?: TripFormData
  notes?: string
}
