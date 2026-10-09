import unittest
from datetime import date

from local_health_adapter import AdapterDisabled, parse_local_page, select_local_fields


STAMP_A = "2031-11-15T20:10:00+08:00"
STAMP_B = "2031-11-15T20:10:06+08:00"
DAY = "2031-11-15"


class LocalHealthAdapterTests(unittest.TestCase):
    def parse(self, page, lines, **kwargs):
        return parse_local_page(
            page, lines, observed_at=kwargs.pop("observed_at", STAMP_A),
            enabled=True, selected_date=kwargs.pop("selected_date", DAY), **kwargs
        )

    def test_adapter_is_disabled_by_default(self):
        with self.assertRaises(AdapterDisabled):
            parse_local_page("active_calories", ["Active calories"], observed_at=STAMP_A,
                             selected_date=DAY)

    def cycle_observations(self):
        palette = {
            "Period": [203, 71, 116],
            "Predicted period": [239, 165, 193],
            "Fertility window": [141, 211, 235],
            "Ovulation": [48, 116, 197],
        }
        observations = [
            {"text": "Cycle Tracker"}, {"text": "Calendar"}, {"text": "Cycle"},
            {"text": "Nov 2031"},
        ]
        weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for column, name in enumerate(weekday_names):
            observations.append({
                "text": name,
                "bbox": [0.08 + column * 0.12, 0.24, 0.04, 0.02],
            })
        for day in range(1, 31):
            weekday = date(2031, 11, day).weekday()
            row = (weekday + (date(2031, 11, 1).weekday())) // 7
            if date(2031, 11, 1).weekday() == 5:
                row = (5 + day - 1) // 7
            x = 0.08 + weekday * 0.12 + 0.01
            y = 0.32 + row * 0.075
            label = "Period" if day in (3, 4) else "Predicted period" if day in (20, 21) else None
            rgb = palette[label] if label else [255, 255, 255]
            if day == 15:
                rgb = [0, 0, 0]
            observations.append({
                "text": str(day), "bbox": [x, y, 0.02, 0.02], "sample_rgb": rgb,
            })
        for label, rgb in palette.items():
            observations.append({"text": label, "sample_rgb": rgb})
        return observations

    def test_cycle_calendar_uses_visible_legend_and_excludes_other_markers(self):
        result = self.parse("cycle_calendar", self.cycle_observations())
        metrics = result["metrics"]
        self.assertEqual(metrics["period_dates"], ["2031-11-03", "2031-11-04"])
        self.assertEqual(metrics["predicted_period_dates"], ["2031-11-20", "2031-11-21"])
        self.assertEqual(metrics["calendar_coverage"], "30/30")
        self.assertTrue(metrics["legend_verified"])

    def test_cycle_calendar_rejects_ambiguous_palette_and_incomplete_grid(self):
        observations = self.cycle_observations()
        next(item for item in observations if item["text"] == "Ovulation")["sample_rgb"] = [203, 71, 116]
        with self.assertRaises(ValueError):
            self.parse("cycle_calendar", observations)
        observations = [item for item in self.cycle_observations()
                       if item.get("text") not in {str(day) for day in range(1, 8)}]
        with self.assertRaises(ValueError):
            self.parse("cycle_calendar", observations)

    def test_local_field_selection_is_specific_and_minimal(self):
        self.assertEqual(select_local_fields("请看经期和预测经期"), ["cycle_calendar"])
        self.assertEqual(select_local_fields("体重、活动卡路里和日照"), [
            "weight_history", "sun_exposure", "active_calories",
        ])
        self.assertEqual(select_local_fields("状态值和步行距离"), [
            "wellness_home", "wellness_detail", "steps_daily_details",
        ])
        self.assertEqual(select_local_fields("最近心率怎么样？"), [])

    def test_weight_history_keeps_each_row_and_time(self):
        result = self.parse("weight_history", [
            "Nov 2031", "61.2 kg", "15, 08:14", "61.2 kg", "15, 08:42",
        ])
        rows = result["metrics"]["weight_history_records"]
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["measured_at_local"] for row in rows], ["08:14", "08:42"])
        self.assertEqual([row["record_date"] for row in rows], [DAY, DAY])
        self.assertEqual([row["unit"] for row in rows], ["kg", "kg"])
        self.assertEqual({row["observed_at"] for row in rows}, {STAMP_A})

    def test_weight_history_rejects_ambiguous_ocr_unit(self):
        result = self.parse("weight_history", ["Nov 2031", "61.24F", "15, 08:14"])
        self.assertEqual(result["metrics"]["weight_history_records"], [])

    def test_sun_duration_and_goal_are_separate(self):
        result = self.parse("sun_exposure", [
            "Sun exposure", "Nov 15, Sat", "15 min", "Goal: 20 min",
        ])
        self.assertEqual(result["metrics"]["duration_min"], 15)
        self.assertEqual(result["metrics"]["goal_min"], 20)
        self.assertEqual(result["date"], DAY)

    def test_wellness_home_point_is_not_daily_average(self):
        result = self.parse("wellness_home", [
            "Mind and Body", "29", "Moderate", "08:14", "Nov 15, Sat",
        ])
        metric = result["metrics"]
        self.assertEqual(metric["kind"], "home_point")
        self.assertEqual(metric["point_time_local"], "08:14")
        self.assertNotIn("stress", metric)

    def test_wellness_detail_keeps_visible_average_label(self):
        result = self.parse("wellness_detail", [
            "Mind and Body", "Nov 15, Sat", "Avg wellness today", "31 Moderate",
        ])
        metric = result["metrics"]
        self.assertEqual(metric["kind"], "daily_average")
        self.assertEqual(metric["label"], "Avg wellness today")
        self.assertEqual(metric["category"], "Moderate")

    def test_active_calories_requires_specific_page_title_and_goal(self):
        result = self.parse("active_calories", [
            "Active calories", "Nov 15, Sat", "Active calories", "123", "300 kcal",
        ])
        metric = result["metrics"]
        self.assertEqual(metric["active_kcal"], 123)
        self.assertEqual(metric["goal_kcal"], 300)
        self.assertEqual(metric["unit"], "kcal")
        with self.assertRaises(ValueError):
            self.parse("active_calories", ["Calories", "123 kcal", "300 kcal"])

    def test_steps_summary_is_day_scoped_and_separate_from_goal(self):
        result = self.parse("steps_daily_summary", [
            "Steps", "Nov 15, Sat", "Steps", "4012", "5000 steps",
        ])
        metric = result["metrics"]
        self.assertEqual(metric["steps"], 4012)
        self.assertEqual(metric["goal_steps"], 5000)
        with self.assertRaises(ValueError):
            self.parse("steps_daily_summary", ["Steps", "4012", "5000 steps"],
                       view_period="Month")

    def test_distance_requires_day_view_and_explicit_distance_label(self):
        result = self.parse("steps_daily_details", [
            "Steps", "Nov 15, Sat", "Details", "Distance", "2.62 km",
        ], observed_at=STAMP_B)
        self.assertEqual(result["metrics"]["distance_km"], 2.62)
        self.assertEqual(result["observed_at"], STAMP_B)
        with self.assertRaises(ValueError):
            self.parse("steps_daily_details", [
                "Steps", "Details", "Distance", "42.0 km",
            ], view_period="Month")

    def test_steps_and_distance_keep_their_own_capture_times(self):
        summary = self.parse("steps_daily_summary", [
            "Steps", "Nov 15, Sat", "Steps", "4012", "5000 steps",
        ], observed_at=STAMP_A)
        details = self.parse("steps_daily_details", [
            "Steps", "Nov 15, Sat", "Details", "Distance", "2.62 km",
        ], observed_at=STAMP_B)
        self.assertEqual(summary["observed_at"], STAMP_A)
        self.assertEqual(details["observed_at"], STAMP_B)
        self.assertNotIn("distance_km", summary["metrics"])
        self.assertNotIn("steps", details["metrics"])

    def test_observed_at_requires_timezone_and_date_must_match_header(self):
        with self.assertRaises(ValueError):
            self.parse("active_calories", ["Active calories", "123"],
                       observed_at="2031-11-15T20:10:00")
        with self.assertRaises(ValueError):
            self.parse("sun_exposure", [
                "Sun exposure", "Nov 16, Sun", "12 min", "Goal: 20 min",
            ])


if __name__ == "__main__":
    unittest.main()
