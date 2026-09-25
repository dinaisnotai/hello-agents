import json
import unittest
from copy import deepcopy

from app.agents.planner_agent import PlannerAgent
from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.models.agent_outputs import WeatherQueryResult
from app.models.quality import ExperienceEvaluation, ExperienceIssue
from app.models.schemas import (
    Attraction,
    DayPlan,
    EvidenceSource,
    Hotel,
    Location,
    RouteSegment,
    TripPlan,
    TripPlanResponse,
    TripRequest,
    WeatherInfo,
)
from app.services.itinerary_quality import ItineraryCompletenessGate
from app.services.repair_controller import RepairProposal
from app.services.spatial_planner import estimate_leg, haversine_meters
from app.services.planning_observability import (
    build_planning_trace,
    refresh_planning_trace,
)


class _EstimatedRouteEvaluator:
    def build_day_routes(self, day, city, request=None):
        if not day.attractions:
            return []
        nodes = []
        if day.hotel and day.hotel.location:
            nodes.append((day.hotel.name, day.hotel.location))
        nodes.extend((item.name, item.location) for item in day.attractions)
        if day.hotel and day.hotel.location:
            nodes.append((day.hotel.name, day.hotel.location))
        result = []
        for (origin_name, origin), (destination_name, destination) in zip(
            nodes, nodes[1:]
        ):
            leg = estimate_leg(origin, destination, day.transportation)
            result.append(
                RouteSegment(
                    day_index=day.day_index,
                    origin=origin_name,
                    destination=destination_name,
                    route_type=leg.mode,
                    distance_meters=haversine_meters(origin, destination),
                    duration_minutes=leg.travel_minutes,
                    walking_distance_meters=leg.walking_meters,
                )
            )
        return result


class _SequenceAgent:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run(self, input_text):
        self.calls.append(json.loads(input_text))
        if len(self.responses) > 1:
            return json.dumps(self.responses.pop(0), ensure_ascii=False)
        return json.dumps(self.responses[0], ensure_ascii=False)


class _DuplicateProposalController:
    def propose(self, plan, request, candidates, hotels, issue):
        candidate = deepcopy(plan)
        duplicate = deepcopy(candidate.days[0].attractions[0])
        candidate.days[issue.day - 1].attractions.append(duplicate)
        return RepairProposal(candidate, "test_inject_duplicate")


class _BudgetBreakingController:
    def __init__(self, costly):
        self.costly = costly

    def propose(self, plan, request, candidates, hotels, issue):
        candidate = deepcopy(plan)
        candidate.days[issue.day - 1].attractions.extend(
            [deepcopy(self.costly), deepcopy(candidates[-1])]
        )
        return RepairProposal(candidate, "test_add_over_budget_pois")


class _AlwaysImprovingButFailingController:
    def propose(self, plan, request, candidates, hotels, issue):
        candidate = deepcopy(plan)
        candidate.overall_suggestions += "x"
        return RepairProposal(candidate, "test_increment_without_resolution")


class _WeatherGlobalMutationController:
    def propose(self, plan, request, candidates, hotels, issue):
        candidate = deepcopy(plan)
        candidate.days[1].attractions.append(deepcopy(candidates[-1]))
        candidate.days[2].attractions = candidate.days[2].attractions[1:]
        candidate.days[0].attractions = list(
            reversed(candidate.days[0].attractions)
        )
        return RepairProposal(
            candidate,
            "ADD_WEATHER_BACKUP:malformed_global_replan",
            ("days[2].weather_backup", "days[2].weather_warning"),
        )


def make_request(**updates):
    data = {
        "city": "北京",
        "start_date": "2026-10-10",
        "end_date": "2026-10-12",
        "travel_days": 3,
        "transportation": "public transit",
        "accommodation": "comfortable hotel",
        "pace": "balanced",
    }
    data.update(updates)
    return TripRequest(**data)


def poi(
    name,
    index,
    *,
    duration=150,
    category="historic",
    ticket=0,
):
    return Attraction(
        name=name,
        poi_id=f"poi-{index}",
        visit_key=f"provider:poi-{index}",
        category=category,
        categories=[category],
        visit_duration=duration,
        suggested_duration_minutes=duration,
        score=90 - index,
        ticket_price=ticket,
        location=Location(
            longitude=116.40 + index * 0.002,
            latitude=39.91 + index * 0.001,
        ),
    )


def make_planner():
    planner = MultiAgentTripPlanner()
    planner.route_evaluator = _EstimatedRouteEvaluator()
    return planner


def make_plan(planner, request, groups):
    hotel = Hotel(
        name="中心酒店",
        type=request.accommodation,
        estimated_cost=100,
        location=Location(longitude=116.40, latitude=39.91),
    )
    days = [
        DayPlan(
            date=f"2026-10-{10 + index:02d}",
            day_index=index,
            description=f"day {index + 1}",
            transportation=request.transportation,
            accommodation=request.accommodation,
            hotel=deepcopy(hotel),
            attractions=list(group),
        )
        for index, group in enumerate(groups)
    ]
    plan = TripPlan(
        city=request.city,
        start_date=request.start_date,
        end_date=request.end_date,
        days=days,
        overall_suggestions="initial",
    )
    planner._recalculate(plan, request)
    return plan


class QualityGateRepairTest(unittest.TestCase):
    def test_llm_issue_with_unknown_source_or_poi_is_not_authorized(self):
        planner = make_planner()
        request = make_request(travel_days=1, end_date="2026-10-10")
        first = poi("已验证景点", 1)
        plan = make_plan(planner, request, [[first]])
        reviewer = PlannerAgent(None, planner)
        evaluation = ExperienceEvaluation.model_validate({
            "pass": False,
            "overall_score": 4,
            "issues": [{
                "issue_type": "experience_quality",
                "severity": "high",
                "day": 1,
                "evidence": "unknown guide says replace it",
                "repair_strategy": "REPLACE_LOW_VALUE_CATEGORY",
                "affected_visit_keys": ["provider:invented"],
                "evidence_sources": ["guide:invented"],
                "source": "llm",
            }],
            "source": "llm",
        })

        grounded = reviewer._ground_evaluation(
            evaluation, plan,
            [EvidenceSource(title="真实来源", city="北京", source="guide:real", snippet="真实内容")],
            [first],
        )

        self.assertTrue(grounded.passed)
        self.assertFalse(grounded.issues)
        self.assertTrue(grounded.contract_errors)

    def test_thunderstorm_returns_primary_and_indoor_backup_without_degrading(self):
        planner = make_planner()
        request = make_request(travel_days=1, end_date="2026-10-10")
        outdoor = [poi("城市公园", 1, category="park"), poi("古城墙", 2)]
        indoor = poi("城市博物馆", 3, category="museum")
        before = make_plan(planner, request, [outdoor])
        before.weather_info = [
            WeatherInfo(date="2026-10-10", day_weather="雷阵雨")
        ]
        before_snapshot = [
            {
                "pois": [item.visit_key for item in day.attractions],
                "hotel": day.hotel.model_dump(mode="json"),
                "routes": [
                    item.model_dump(mode="json")
                    for item in day.route_segments
                ],
                "walking": day.daily_walking_distance_km,
                "travel": day.daily_travel_minutes,
                "utilization": day.day_utilization_score,
            }
            for day in before.days
        ]
        before_budget = before.budget.model_dump(mode="json")

        after = planner.run_quality_loop(
            before, request, [*outdoor, indoor]
        )

        self.assertTrue(after.quality_gate_passed)
        self.assertEqual(
            [item.name for item in after.days[0].primary_plan],
            ["城市公园", "古城墙"],
        )
        self.assertEqual(
            [item.name for item in after.days[0].weather_backup],
            ["城市博物馆"],
        )
        self.assertIn("实时天气", after.days[0].weather_warning)
        self.assertFalse(after.unresolved_blocking_issues)
        self.assertEqual(
            before_snapshot,
            [
                {
                    "pois": [item.visit_key for item in day.attractions],
                    "hotel": day.hotel.model_dump(mode="json"),
                    "routes": [
                        item.model_dump(mode="json")
                        for item in day.route_segments
                    ],
                    "walking": day.daily_walking_distance_km,
                    "travel": day.daily_travel_minutes,
                    "utilization": day.day_utilization_score,
                }
                for day in after.days
            ],
        )
        self.assertEqual(before_budget, after.budget.model_dump(mode="json"))
        self.assertEqual(
            after.repair_history[0].actual_modified_fields,
            [
                "days[0].weather_backup",
                "days[0].weather_warning",
            ],
        )

    def test_weather_scope_rolls_back_the_reported_global_replan(self):
        planner = make_planner()
        planner.repair_controller = _WeatherGlobalMutationController()
        request = make_request()
        groups = [
            [poi("故宫", 1), poi("天坛", 2)],
            [poi("颐和园", 3), poi("首都博物馆", 4, category="museum")],
            [poi("国子监", 5), poi("正阳门箭楼", 6)],
        ]
        badaling = poi("八达岭长城", 7, duration=300)
        before = make_plan(planner, request, groups)
        before.weather_info = [
            WeatherInfo(date="2026-10-12", day_weather="雷阵雨")
        ]
        itinerary_before = [
            [item.name for item in day.attractions] for day in before.days
        ]

        after = planner.run_quality_loop(
            before,
            request,
            [item for group in groups for item in group] + [badaling],
        )

        self.assertTrue(after.quality_gate_passed)
        self.assertEqual(
            itinerary_before,
            [[item.name for item in day.attractions] for day in after.days],
        )
        record = after.repair_history[0]
        self.assertFalse(record.accepted)
        self.assertEqual(
            record.action_result,
            "rolled_back_before_recalculation",
        )
        self.assertIn("Mutation scope violation", record.rollback_reason)
        self.assertIn("days[1].attractions", record.actual_modified_fields)
        self.assertIn("days[2].attractions", record.actual_modified_fields)
        self.assertEqual(record.constraint_delta["added"], [])

    def test_weather_without_indoor_candidate_keeps_primary_and_warning(self):
        planner = make_planner()
        request = make_request(travel_days=1, end_date="2026-10-10")
        outdoor = [poi("城市公园", 1, category="park"), poi("古城墙", 2)]
        before = make_plan(planner, request, [outdoor])
        before.weather_info = [
            WeatherInfo(date="2026-10-10", day_weather="高温")
        ]

        after = planner.run_quality_loop(before, request, outdoor)

        self.assertTrue(after.quality_gate_passed)
        self.assertEqual(after.days[0].attractions, after.days[0].primary_plan)
        self.assertFalse(after.days[0].weather_backup)
        self.assertIn("没有可靠室内备选", after.days[0].weather_warning)
        self.assertTrue(after.unresolved_non_blocking_issues)

    def test_unknown_repair_strategy_is_explicit_contract_error(self):
        planner = make_planner()
        request = make_request(travel_days=1, end_date="2026-10-10")
        plan = make_plan(
            planner, request, [[poi("A", 1), poi("B", 2, category="park")]]
        )
        reviewer = PlannerAgent(None, planner)
        reviewer.agent = _SequenceAgent(
            [
                {
                    "pass": False,
                    "overall_score": 5,
                    "issues": [
                        {
                            "issue_type": "weather_risk",
                            "severity": "warning",
                            "day": 1,
                            "evidence": "rain",
                            "repair_strategy": "unbounded_free_text_strategy",
                            "source": "llm",
                        }
                    ],
                    "source": "llm",
                }
            ]
        )

        after = reviewer.review_plan(
            request,
            plan,
            WeatherQueryResult(),
            [],
            available_attractions=[],
        )

        self.assertTrue(after.quality_evaluation.contract_errors)
        self.assertIn(
            "unsupported repair_strategy",
            after.quality_evaluation.contract_errors[0],
        )
        self.assertEqual(after.quality_evaluation.source, "deterministic")
        self.assertTrue(after.quality_gate_passed)

    def test_known_review_strategy_alias_is_normalized_before_repair(self):
        planner = make_planner()
        request = make_request(travel_days=1, end_date="2026-10-10")
        plan = make_plan(
            planner, request, [[poi("A", 1), poi("B", 2, category="park")]]
        )
        reviewer = PlannerAgent(None, planner)
        reviewer.agent = _SequenceAgent(
            [
                {
                    "pass": False,
                    "overall_score": 5,
                    "issues": [
                        {
                            "issue_type": "weather_risk",
                            "severity": "warning",
                            "day": 1,
                            "evidence": "rain",
                            "repair_strategy": "swap_weather_sensitive_attractions",
                            "source": "llm",
                        }
                    ],
                    "source": "llm",
                }
            ]
        )

        evaluation = reviewer.evaluate_experience(
            request,
            plan,
            WeatherQueryResult(),
            [],
            available_attractions=[],
        )

        self.assertIsNotNone(evaluation)
        self.assertEqual(
            evaluation.issues[0].repair_strategy.value,
            "SWAP_WITH_INDOOR_CANDIDATE",
        )

    def test_unrepairable_soft_issue_returns_best_effort_itinerary(self):
        planner = make_planner()
        request = make_request(travel_days=1, end_date="2026-10-10")
        selected = [poi("A", 1), poi("B", 2, category="park")]
        before = make_plan(planner, request, [selected])

        def evaluator(plan):
            return ExperienceEvaluation(
                **{
                    "pass": False,
                    "overall_score": 7,
                    "issues": [
                        ExperienceIssue(
                            issue_type="preference_alignment",
                            severity="high",
                            day=1,
                            evidence="Preference match is not ideal",
                            repair_strategy="REPLACE_LOW_VALUE_CATEGORY",
                            source="llm",
                        )
                    ],
                    "source": "llm",
                }
            )

        after = planner.run_quality_loop(
            before,
            request,
            selected,
            external_evaluator=evaluator,
        )

        self.assertTrue(after.quality_gate_passed)
        self.assertTrue(after.best_effort)
        self.assertEqual(len(after.days[0].attractions), 2)
        self.assertTrue(after.unresolved_non_blocking_issues)
        self.assertIsNone(after.degraded_reason)

    def test_unrepairable_hard_issue_is_degraded_but_nonempty_response(self):
        planner = make_planner()
        request = make_request(travel_days=1, end_date="2026-10-10")
        before = make_plan(planner, request, [[]])

        after = planner.run_quality_loop(before, request, [])
        payload = after.model_dump(mode="json")

        self.assertFalse(after.quality_gate_passed)
        self.assertTrue(after.degraded_reason)
        self.assertIn("days", payload)
        self.assertEqual(len(payload["days"]), 1)
        self.assertTrue(payload["unresolved_blocking_issues"])
        self.assertTrue(payload["suggested_alternatives"])
        self.assertIsNotNone(payload["days"][0]["primary_plan"])
        response = TripPlanResponse(
            success=False,
            message="degraded best-effort",
            data=after,
        ).model_dump(mode="json")
        self.assertIsNotNone(response["data"])
        self.assertTrue(response["data"]["unresolved_quality_issues"])

    def test_repeated_failed_issue_does_not_consume_more_iterations(self):
        planner = make_planner()
        request = make_request(travel_days=1, end_date="2026-10-10")
        selected = [poi("A", 1), poi("B", 2, category="park")]
        before = make_plan(planner, request, [selected])

        def evaluator(plan):
            return ExperienceEvaluation(
                **{
                    "pass": False,
                    "overall_score": 6,
                    "issues": [
                        ExperienceIssue(
                            issue_type="experience_quality",
                            severity="warning",
                            day=1,
                            evidence="Same reviewer observation",
                            repair_strategy="ADD_NEARBY_COMPLEMENTARY_POI",
                            source="llm",
                        )
                    ],
                    "source": "llm",
                }
            )

        after = planner.run_quality_loop(
            before,
            request,
            selected,
            external_evaluator=evaluator,
        )

        self.assertEqual(len(after.repair_history), 1)
        self.assertEqual(
            after.repair_history[0].issue_fingerprint,
            after.unresolved_quality_issues[0].fingerprint,
        )
        self.assertFalse(after.repair_history[0].accepted)
        self.assertTrue(after.quality_gate_passed)

    def test_third_empty_day_is_filled_from_remaining_candidates(self):
        planner = make_planner()
        request = make_request()
        selected = [poi("A", 1), poi("B", 2), poi("C", 3), poi("D", 4)]
        remaining = [poi("E", 5), poi("F", 6, category="park")]
        before = make_plan(
            planner,
            request,
            [selected[:2], selected[2:], []],
        )

        after = planner.run_quality_loop(
            before,
            request,
            [*selected, *remaining],
        )

        self.assertTrue(after.quality_gate_passed)
        self.assertGreaterEqual(len(after.days[2].attractions), 2)
        self.assertTrue(after.constraint_report.passed)
        trace = build_planning_trace(
            request,
            after,
            candidates=[*selected, *remaining],
        )
        self.assertTrue(trace.quality_gate.result["pass"])
        self.assertTrue(trace.quality_gate.repair_iterations)
        self.assertEqual(
            trace.quality_gate.final_reason,
            "quality_gate_passed",
        )
        after.observability_trace = trace
        refresh_planning_trace(after)
        self.assertEqual(
            after.observability_trace.quality_gate.input["candidate_count"],
            len([*selected, *remaining]),
        )

    def test_add_unused_candidate_never_redistributes_across_days(self):
        planner = make_planner()
        request = make_request()
        all_day = poi("全日景区", 1, duration=240)
        others = [
            poi(
                f"P{index}",
                index,
                category="park" if index % 2 == 0 else "museum",
            )
            for index in range(2, 6)
        ]
        before = make_plan(
            planner,
            request,
            [[all_day, others[0], others[1]], others[2:], []],
        )

        after = planner.run_quality_loop(
            before,
            request,
            [all_day, *others],
        )

        self.assertFalse(after.quality_gate_passed)
        self.assertFalse(after.days[2].attractions)
        self.assertEqual(
            [[item.name for item in day.attractions] for day in after.days],
            [
                [all_day.name, others[0].name, others[1].name],
                [others[2].name, others[3].name],
                [],
            ],
        )
        self.assertTrue(after.best_effort)

    def test_structured_llm_issue_triggers_real_repair(self):
        planner = make_planner()
        request = make_request(travel_days=1, end_date="2026-10-10")
        first = poi("短活动", 1, duration=90)
        replacement = poi("附近公园", 2, category="park")
        before = make_plan(planner, request, [[first]])
        before.observability_trace = build_planning_trace(
            request,
            before,
            candidates=[first, replacement],
        )
        fail = {
            "pass": False,
            "overall_score": 5.5,
            "issues": [
                {
                    "issue_type": "underfilled_day",
                    "severity": "critical",
                    "day": 1,
                    "evidence": "Day 1 has only one short activity",
                    "repair_strategy": "ADD_NEARBY_COMPLEMENTARY_POI",
                    "source": "llm",
                }
            ],
            "source": "llm",
        }
        passed = {
            "pass": True,
            "overall_score": 9,
            "issues": [],
            "source": "llm",
        }
        reviewer = PlannerAgent(None, planner)
        reviewer.agent = _SequenceAgent([fail, passed])

        after = reviewer.review_plan(
            request,
            before,
            WeatherQueryResult(),
            [],
            available_attractions=[first, replacement],
            hotel_candidates=[before.days[0].hotel],
        )

        self.assertEqual(
            [item.name for item in after.days[0].attractions],
            ["短活动", "附近公园"],
        )
        self.assertTrue(after.repair_history)
        self.assertEqual(
            after.repair_history[0].trigger_issue.source,
            "llm",
        )
        self.assertGreaterEqual(len(reviewer.agent.calls[0]["plan"]["days"]), 1)
        self.assertTrue(reviewer.agent.calls[0]["hotels"])
        self.assertIn(
            "constraint_report",
            reviewer.agent.calls[0]["plan"],
        )
        self.assertTrue(
            reviewer.agent.calls[0]["observability"]["candidate_pois"]
        )
        self.assertTrue(
            after.observability_trace.quality_gate.reviewer_issues
        )
        self.assertTrue(
            any(
                call["review_context"]["previous_attempts"]
                for call in reviewer.agent.calls[1:]
            )
        )

    def test_duplicate_created_by_repair_is_intercepted(self):
        planner = make_planner()
        planner.repair_controller = _DuplicateProposalController()
        request = make_request()
        first = poi("故宫", 1)
        before = make_plan(planner, request, [[first], [poi("B", 2)], []])

        after = planner.run_quality_loop(before, request, [first, poi("B", 2)])

        keys = [
            item.visit_key
            for day in after.days
            for item in day.attractions
        ]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertFalse(after.repair_history[0].accepted)

    def test_repair_that_breaks_budget_is_rejected(self):
        planner = make_planner()
        costly = poi("昂贵景点", 8, ticket=1000)
        filler = poi("普通景点", 9)
        planner.repair_controller = _BudgetBreakingController(costly)
        request = make_request(budget_limit=900)
        before = make_plan(
            planner,
            request,
            [[poi("A", 1), poi("B", 2)], [poi("C", 3), poi("D", 4)], []],
        )

        after = planner.run_quality_loop(
            before,
            request,
            [*before.days[0].attractions, *before.days[1].attractions, costly, filler],
        )

        self.assertFalse(after.repair_history[0].accepted)
        self.assertIn(
            "hard-constraint validation failed",
            after.repair_history[0].rejection_reason,
        )
        self.assertLessEqual(after.budget.total, request.budget_limit)

    def test_same_soft_issue_is_attempted_only_once(self):
        planner = make_planner()
        planner.repair_controller = _AlwaysImprovingButFailingController()
        request = make_request(travel_days=1, end_date="2026-10-10")
        before = make_plan(
            planner,
            request,
            [[poi("A", 1), poi("B", 2)]],
        )

        def evaluator(plan):
            score = min(9, 4 + plan.overall_suggestions.count("x"))
            return ExperienceEvaluation(
                **{
                    "pass": False,
                    "overall_score": score,
                    "issues": [
                        ExperienceIssue(
                            issue_type="experience_quality",
                            severity="critical",
                            evidence="Reviewer issue remains unresolved",
                            repair_strategy="ADD_NEARBY_COMPLEMENTARY_POI",
                            source="llm",
                        )
                    ],
                    "source": "llm",
                }
            )

        after = planner.run_quality_loop(
            before,
            request,
            [*before.days[0].attractions],
            external_evaluator=evaluator,
        )

        self.assertEqual(len(after.repair_history), 1)
        self.assertTrue(after.quality_gate_passed)
        self.assertTrue(after.unresolved_quality_issues)
        self.assertIsNone(after.failure_reason)

    def test_late_arrival_allows_reasonable_empty_first_day(self):
        planner = make_planner()
        request = make_request(arrival_time="19:00")
        plan = make_plan(
            planner,
            request,
            [
                [],
                [poi("A", 1), poi("B", 2, category="park")],
                [poi("C", 3, category="museum"), poi("D", 4)],
            ],
        )

        evaluation = ItineraryCompletenessGate().evaluate(
            request,
            plan,
            [item for day in plan.days for item in day.attractions],
        )

        self.assertFalse(
            any(item.issue_type == "empty_day" for item in evaluation.issues)
        )
        self.assertTrue(plan.constraint_report.passed)

    def test_single_all_day_attraction_is_not_underfilled(self):
        planner = make_planner()
        request = make_request(travel_days=1, end_date="2026-10-10")
        plan = make_plan(
            planner,
            request,
            [[poi("大型主题乐园", 1, duration=300)]],
        )

        evaluation = ItineraryCompletenessGate().evaluate(
            request,
            plan,
            plan.days[0].attractions,
        )

        self.assertFalse(
            any(
                item.issue_type == "underfilled_day"
                for item in evaluation.issues
            )
        )

    def test_low_landmark_coverage_adds_existing_core_candidate(self):
        planner = make_planner()
        request = make_request(
            travel_days=1,
            end_date="2026-10-10",
            first_visit=True,
        )
        existing = [
            poi("社区公园", 1, category="park"),
            poi("历史街区", 2),
        ]
        core = poi("城市核心地标", 3)
        core.is_core_landmark = True
        before = make_plan(planner, request, [existing])

        after = planner.run_quality_loop(
            before,
            request,
            [*existing, core],
        )

        self.assertTrue(after.quality_gate_passed)
        self.assertIn(
            "城市核心地标",
            [item.name for item in after.days[0].attractions],
        )
        self.assertIn(
            "add_core_landmark",
            after.repair_history[0].selected_action,
        )

    def test_budget_violation_selects_affordable_existing_hotel(self):
        planner = make_planner()
        request = make_request(budget_limit=1000)
        groups = [
            [poi("A", 1), poi("B", 2, category="park")],
            [poi("C", 3, category="museum"), poi("D", 4)],
            [poi("E", 5), poi("F", 6, category="park")],
        ]
        before = make_plan(planner, request, groups)
        for day in before.days:
            # Two nights must exceed the 1000 budget even with free walking.
            day.hotel.estimated_cost = 600
        planner._recalculate(before, request)
        affordable = Hotel(
            name="经济酒店",
            type=request.accommodation,
            estimated_cost=100,
            location=Location(longitude=116.40, latitude=39.91),
        )

        after = planner.run_quality_loop(
            before,
            request,
            [item for group in groups for item in group],
            hotel_candidates=[affordable],
        )

        self.assertTrue(after.quality_gate_passed)
        self.assertTrue(after.constraint_report.passed)
        self.assertTrue(
            all(day.hotel.name == "经济酒店" for day in after.days)
        )
        self.assertIn(
            "select_more_affordable_hotel",
            after.repair_history[0].selected_action,
        )

    def test_long_transport_reselects_hotel_and_reclusters(self):
        planner = make_planner()
        request = make_request(
            travel_days=1,
            end_date="2026-10-10",
            pace="packed",
        )
        full_day = poi("全日景区", 1, duration=300)
        before = make_plan(planner, request, [[full_day]])
        before.days[0].hotel = Hotel(
            name="远郊酒店",
            type=request.accommodation,
            estimated_cost=300,
            location=Location(longitude=114.40, latitude=39.91),
        )
        planner._recalculate(before, request)
        nearby = Hotel(
            name="景区附近酒店",
            type=request.accommodation,
            estimated_cost=300,
            location=Location(
                longitude=full_day.location.longitude,
                latitude=full_day.location.latitude,
            ),
        )

        after = planner.run_quality_loop(
            before,
            request,
            [full_day],
            hotel_candidates=[before.days[0].hotel, nearby],
        )

        self.assertTrue(after.quality_gate_passed)
        self.assertEqual(after.days[0].hotel.name, "景区附近酒店")
        self.assertLess(
            after.days[0].daily_travel_minutes,
            before.days[0].daily_travel_minutes,
        )


if __name__ == "__main__":
    unittest.main()
