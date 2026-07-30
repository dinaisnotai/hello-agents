import unittest

from app.models.schemas import Attraction, DayPlan, Location, TripPlan, WeatherInfo
from app.services.user_warning_service import curate_user_warnings


def poi(name, category):
    return Attraction(
        name=name, category=category, categories=[category],
        location=Location(longitude=116.4, latitude=39.9),
    )


class UserWarningServiceTest(unittest.TestCase):
    def test_only_final_primary_pois_produce_weather_warning(self):
        tiantan = poi("天坛公园", "park")
        museum = poi("国家博物馆", "museum")
        day = DayPlan(
            date="2026-07-30", day_index=0, description="test",
            transportation="public transit", accommodation="hotel",
            attractions=[tiantan, museum], primary_plan=[tiantan, museum],
        )
        plan = TripPlan(
            city="北京", start_date="2026-07-30", end_date="2026-07-30",
            days=[day], overall_suggestions="test",
            weather_info=[WeatherInfo(date="2026-07-30", day_weather="雷阵雨")],
            risk_warnings=[
                "ExperienceEvaluator: 雷阵雨 affects outdoor primary visits: 太庙",
                "已合并重复游览实体：中国国家博物馆",
                "Day1移动距离较长（44.1km）",
                "Reviewer发现部分约束未完全满足",
                "天气存在降雨/恶劣风险，建议把室外景点和博物馆类景点互换。",
            ],
        )

        curate_user_warnings(plan)

        self.assertEqual(len(plan.risk_warnings), 1)
        self.assertIn("天坛公园", plan.risk_warnings[0])
        self.assertNotIn("太庙", plan.risk_warnings[0])
        self.assertNotIn("ExperienceEvaluator", plan.risk_warnings[0])

    def test_keeps_short_actionable_chinese_weather_notice_once(self):
        park = poi("颐和园", "park")
        day = DayPlan(
            date="2026-07-30", day_index=0, description="test",
            transportation="public transit", accommodation="hotel",
            attractions=[park], primary_plan=[park],
            weather_warning="雷阵雨：保留主计划，出发前查看实时天气。",
        )
        plan = TripPlan(
            city="北京", start_date="2026-07-30", end_date="2026-07-30",
            days=[day], overall_suggestions="test",
            weather_info=[WeatherInfo(date="2026-07-30", day_weather="雷阵雨")],
            risk_warnings=["雷阵雨：保留主计划，出发前查看实时天气。"],
        )

        curate_user_warnings(plan)

        self.assertEqual(plan.risk_warnings, ["雷阵雨：保留主计划，出发前查看实时天气。"])


if __name__ == "__main__":
    unittest.main()
