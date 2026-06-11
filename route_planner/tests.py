from __future__ import annotations

from django.test import SimpleTestCase
from django.urls import reverse

from .services import (
    CandidateStation,
    Coordinate,
    Station,
    build_fuel_plan,
    sample_route,
)


class FuelPlannerTests(SimpleTestCase):
    def test_chooses_lower_cost_reachable_station_chain(self) -> None:
        candidates = [
            self._candidate("expensive", 250.0, 4.90),
            self._candidate("cheap", 490.0, 3.10),
            self._candidate("mid", 880.0, 4.00),
        ]

        plan = build_fuel_plan(1000.0, candidates)

        self.assertEqual(plan["total_gallons"], 100.0)
        self.assertEqual([stop["name"] for stop in plan["stops"]], ["cheap", "mid"])
        self.assertNotIn("expensive", [stop["name"] for stop in plan["stops"]])
        self.assertEqual(plan["purchases"][-1]["name"], "mid")

    def test_short_route_without_candidates_uses_initial_fuel_estimate(self) -> None:
        plan = build_fuel_plan(120.0, [])

        self.assertEqual(plan["stops"], [])
        self.assertEqual(plan["total_gallons"], 12.0)
        self.assertEqual(plan["purchases"][0]["type"], "initial")

    def test_route_sampling_keeps_requested_route_distance_scale(self) -> None:
        samples = sample_route(
            [
                Coordinate(40.0, -75.0),
                Coordinate(40.0, -74.0),
            ],
            route_distance_miles=100.0,
        )

        self.assertEqual(samples[0].mile, 0.0)
        self.assertEqual(samples[-1].mile, 100.0)
        self.assertGreater(len(samples), 10)

    def _candidate(self, station_id: str, route_mile: float, price: float) -> CandidateStation:
        station = Station(
            station_id=station_id,
            name=station_id,
            address="I-80",
            city="Test City",
            state="PA",
            rack_id="1",
            price=price,
            coordinate=Coordinate(40.0, -75.0),
        )
        return CandidateStation(
            station=station,
            route_mile=route_mile,
            distance_from_route_miles=1.0,
        )


class RoutePlanViewTests(SimpleTestCase):
    def test_missing_locations_returns_400(self) -> None:
        response = self.client.post(
            reverse("route-plan"),
            data={},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
