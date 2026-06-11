from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase
from django.urls import reverse

from .services import (
    CandidateStation,
    Coordinate,
    choose_fuel_stops,
    fuel_leg_cost,
    sample_route,
)


class FuelStationStub:
    def __init__(self, station_id: str, price: float) -> None:
        self.id = hash(station_id) % 100000
        self.opis_truckstop_id = station_id
        self.truckstop_name = station_id
        self.address = "I-80"
        self.city = "Test City"
        self.state = "PA"
        self.rack_id = "1"
        self.retail_price = Decimal(str(price))
        self.latitude = Decimal("40.0")
        self.longitude = Decimal("-75.0")


class FuelPlannerTests(SimpleTestCase):
    def test_chooses_effective_cost_not_just_price(self) -> None:
        candidates = [
            self._candidate("cheap_far", 250.0, 3.10, 30.0),
            self._candidate("slightly_more_near", 300.0, 3.20, 1.0),
        ]

        plan = choose_fuel_stops(760.0, candidates, corridor_radius_miles=50.0)

        self.assertEqual(plan["selected_fuel_stops"][0]["truckstop_name"], "slightly_more_near")
        self.assertGreater(plan["selected_fuel_stops"][0]["detour_cost_usd"], 0)

    def test_short_route_needs_no_fuel_stop_when_starting_full(self) -> None:
        plan = choose_fuel_stops(400.0, [], corridor_radius_miles=10.0)

        self.assertEqual(plan["selected_fuel_stops"], [])
        self.assertEqual(plan["total_fuel_cost_usd"], 0.0)
        self.assertEqual(plan["en_route_fuel_cost_usd"], 0.0)
        self.assertEqual(plan["selected_stop_purchase_cost_usd"], 0.0)
        self.assertEqual(plan["total_route_gallons"], 40.0)
        self.assertTrue(plan["assumptions"]["starts_with_full_tank"])

    def test_full_tank_route_uses_one_stop_for_760_miles(self) -> None:
        candidates = [
            self._candidate("too_early", 200.0, 2.50, 1.0),
            self._candidate("needed_stop", 320.0, 3.25, 1.0),
        ]

        plan = choose_fuel_stops(760.0, candidates, corridor_radius_miles=10.0)

        self.assertEqual(len(plan["selected_fuel_stops"]), 1)
        self.assertEqual(plan["selected_fuel_stops"][0]["truckstop_name"], "needed_stop")
        self.assertEqual(plan["selected_fuel_stops"][0]["next_route_mile"], 760.0)
        self.assertEqual(plan["minimum_required_stops"], 1)
        self.assertEqual(plan["planned_stop_count"], 1)

    def test_tries_extra_stop_when_minimum_stop_count_is_infeasible(self) -> None:
        candidates = [
            self._candidate("first_stop", 300.0, 3.10, 1.0),
            self._candidate("second_stop", 700.0, 3.25, 1.0),
        ]

        plan = choose_fuel_stops(900.0, candidates, corridor_radius_miles=10.0)

        self.assertEqual(plan["minimum_required_stops"], 1)
        self.assertEqual(plan["planned_stop_count"], 2)
        self.assertEqual(
            [stop["truckstop_name"] for stop in plan["selected_fuel_stops"]],
            ["first_stop", "second_stop"],
        )

    def test_full_tank_route_uses_two_stops_for_1200_miles(self) -> None:
        candidates = [
            self._candidate("first_stop", 420.0, 3.10, 1.0),
            self._candidate("second_stop", 820.0, 3.25, 1.0),
        ]

        plan = choose_fuel_stops(1200.0, candidates, corridor_radius_miles=10.0)

        self.assertEqual(len(plan["selected_fuel_stops"]), 2)
        self.assertEqual(plan["selected_fuel_stops"][0]["next_route_mile"], 820.0)
        self.assertEqual(plan["selected_fuel_stops"][1]["next_route_mile"], 1200.0)

    def test_fuel_leg_cost_includes_detour(self) -> None:
        candidate = self._candidate("detour", 300.0, 3.00, 5.0)

        self.assertEqual(fuel_leg_cost(candidate, 100.0), 33.0)

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

    def _candidate(
        self,
        station_id: str,
        route_mile: float,
        price: float,
        distance_from_route: float,
    ) -> CandidateStation:
        return CandidateStation(
            station=FuelStationStub(station_id, price),
            route_mile=route_mile,
            distance_from_route_miles=distance_from_route,
        )


class RoutePlanViewTests(SimpleTestCase):
    def test_missing_locations_returns_400(self) -> None:
        response = self.client.post(
            reverse("route-plan"),
            data={},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["status_code"], 400)
        self.assertIn("start_location", payload["message"])
        self.assertIn("finish_location", payload["message"])
