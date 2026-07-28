import unittest

from app.models.schemas import Attraction, Location, TripRequest
from app.services.attraction_scorer import AttractionScorer
from app.services.poi_metadata_service import (
    build_preference_profile,
    enrich_attraction,
)


def request(**updates):
    data = {
        "city": "北京",
        "start_date": "2026-08-01",
        "end_date": "2026-08-03",
        "travel_days": 3,
        "transportation": "公共交通",
        "accommodation": "经济型酒店",
        "pace": "balanced",
    }
    data.update(updates)
    return TripRequest(**data)


def attraction(name, longitude, **updates):
    data = {
        "name": name,
        "location": Location(longitude=longitude, latitude=39.9),
        "categories": ["museum"],
        "tags": ["museum"],
        "popularity": 7,
        "visit_duration": 120,
    }
    data.update(updates)
    return Attraction(**data)


class AttractionScorerTest(unittest.TestCase):
    def test_preference_match_outranks_unmatched_category(self):
        scorer = AttractionScorer()
        hotel = Location(longitude=116.40, latitude=39.9)
        food = attraction(
            "美食街", 116.41, categories=["shopping"], tags=["food", "shopping"]
        )
        museum = attraction("博物馆", 116.41)

        scorer.score_candidates([food, museum], request(preferences=["food"]), hotel, 540)

        self.assertGreater(food.score, museum.score)
        self.assertGreater(
            food.score_breakdown["preference_score"],
            museum.score_breakdown["preference_score"],
        )

    def test_far_candidate_is_penalized_in_route_context(self):
        scorer = AttractionScorer()
        origin = Location(longitude=116.40, latitude=39.9)
        near = attraction("附近公园", 116.41, categories=["park"], tags=["park"], score=70)
        far = attraction("远郊景点", 117.30, categories=["park"], tags=["park"], score=70)

        self.assertGreater(
            scorer.route_increment_score(near, [], origin, 540),
            scorer.route_increment_score(far, [], origin, 540),
        )

    def test_repeated_category_receives_diversity_penalty(self):
        scorer = AttractionScorer()
        hotel = Location(longitude=116.40, latitude=39.9)
        first = attraction("博物馆A", 116.41, score=70)
        second = attraction("博物馆B", 116.42, score=70)

        score_without_repeat = scorer.route_increment_score(second, [], hotel, 540)
        score_with_repeat = scorer.route_increment_score(second, [first], hotel, 540)

        self.assertGreater(score_without_repeat, score_with_repeat)

    def test_context_scoring_preserves_curated_classic_ranking(self):
        scorer = AttractionScorer()
        trip_request = request(preferences=["历史"])
        profile = build_preference_profile(trip_request)
        palace = enrich_attraction(
            attraction("故宫博物院", 116.397), trip_request, profile
        )
        niche = enrich_attraction(
            attraction("北京艺术博物馆", 116.40), trip_request, profile
        )

        scorer.score_candidates(
            [palace, niche],
            trip_request,
            Location(longitude=116.397, latitude=39.9),
            540,
        )

        self.assertGreater(palace.score, niche.score)
        self.assertEqual(palace.score_breakdown["classic_bonus"], 30)
        self.assertEqual(niche.score_breakdown["niche_penalty"], -40)


if __name__ == "__main__":
    unittest.main()
