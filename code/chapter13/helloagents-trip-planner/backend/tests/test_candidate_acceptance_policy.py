import unittest

from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.models.schemas import Attraction, DayPlan, Location, TripRequest
from app.services.candidate_acceptance_policy import CandidateAcceptancePolicy


def request():
    return TripRequest(
        city="北京", start_date="2026-10-10", end_date="2026-10-10",
        travel_days=1, transportation="public transit",
        accommodation="budget hotel", first_visit=True,
    )


def poi(name, score, role, category="museum"):
    return Attraction(
        name=name, poi_id=name, visit_key=f"provider:{name}", score=score,
        selection_role=role,
        is_core_landmark=role == "core_landmark",
        category=category, categories=[category], popularity=5,
        location=Location(longitude=116.40, latitude=39.90),
    )


class CandidateAcceptancePolicyTest(unittest.TestCase):
    def test_low_score_niche_is_rejected_when_higher_major_is_feasible(self):
        major = poi("国家博物馆", 93.6, "major_attraction")
        niche = poi("北京警察博物馆", 23.6, "niche_attraction")
        decision = CandidateAcceptancePolicy().evaluate(
            niche, pool=[major, niche], selected=[], request=request(),
            entry_point="underfilled_day_fill",
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "higher_value_core_or_major_feasible")
        self.assertEqual(decision.higher_value_alternatives, ("国家博物馆",))

    def test_planner_fill_cannot_trade_portfolio_quality_for_utilization(self):
        planner = MultiAgentTripPlanner()
        major = poi("正阳门箭楼", 55.0, "major_attraction", "historic")
        niche = poi("北京警察博物馆", 23.6, "niche_attraction", "museum")
        day = DayPlan(
            date="2026-10-10", day_index=0, description="test",
            transportation="public transit", accommodation="budget hotel",
            attractions=[],
        )
        self.assertFalse(
            planner._try_add_attraction(day, niche, request(), [major, niche])
        )
        self.assertEqual(
            niche.selection_trace[-1]["reason"],
            "higher_value_core_or_major_feasible",
        )


if __name__ == "__main__":
    unittest.main()
