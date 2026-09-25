"""Regression cases at the map-to-candidate boundary, without external calls."""
import json
import unittest
from types import SimpleNamespace

from app.agents.trip_planner_agent import POICollector, _POICandidate
from app.services.amap_service import AmapService
from app.services.itinerary_quality import ItineraryCompletenessGate
from app.services.place_name_service import place_names_match
from app.services.venue_policy import venue_kind


class VenueAdmissionTest(unittest.TestCase):
    def test_map_query_cannot_manufacture_a_venue_type(self):
        raw = json.dumps({"pois": [
            {"id": "unknown", "name": "甲乙中心", "location": "116.4,39.9"},
            {"id": "museum", "name": "医院旧址博物馆", "type": "科教文化服务;博物馆", "location": "116.4,39.9"},
            {"id": "hospital", "name": "天坛医院", "type": "医疗保健服务", "location": "116.4,39.9"},
        ]})
        # Parsing and filtering only: no credentials or network required.
        service = AmapService.__new__(AmapService)
        pois = service._parse_pois(raw, "景点", "北京")
        self.assertEqual(pois[0].type, "")
        candidates = [_POICandidate(poi=poi) for poi in pois]
        kept = POICollector(service)._filter_candidates(candidates, SimpleNamespace(avoid_categories=[]))
        self.assertEqual([item.poi.id for item in kept], ["museum"])

    def test_specific_provider_type_and_generic_label_have_different_authority(self):
        cases = [
            ("北京饭店", "住宿服务;宾馆酒店", "hotel"),
            ("医院旧址博物馆", "科教文化服务;博物馆", "attraction"),
            ("天坛医院", "景点", "medical"),
            ("故宫售票处", "风景名胜", "infrastructure"),
            ("甲乙中心", "", "unknown"),
            ("甲乙中心（东区）", "", "unknown"),
            ("故宫", "", "attraction"),
            ("天坛公园", "医疗保健服务", "medical"),
            ("某公园", "风景名胜;附属设施", "infrastructure"),
        ]
        for name, kind, expected in cases:
            with self.subTest(name=name, kind=kind):
                self.assertEqual(venue_kind(name, kind), expected)

    def test_alias_matching_does_not_promote_hospital_to_required_attraction(self):
        self.assertTrue(place_names_match("国博", "中国国家博物馆"))
        self.assertTrue(place_names_match("故宫", "故宫博物院"))
        self.assertFalse(place_names_match("天坛", "首都医科大学附属北京天坛医院"))

    def test_final_gate_also_rejects_unknown_venue(self):
        plan = SimpleNamespace(days=[SimpleNamespace(day_index=0, attractions=[
            SimpleNamespace(name="甲乙中心", category="", categories=[])
        ], meals=[], hotel=None)])
        issues = []
        ItineraryCompletenessGate._evaluate_product_contract(
            SimpleNamespace(preferences=[], accommodation="经济型酒店"), plan, issues
        )
        self.assertTrue(any(issue.is_blocking and "甲乙中心" in issue.evidence for issue in issues))


if __name__ == "__main__":
    unittest.main()
