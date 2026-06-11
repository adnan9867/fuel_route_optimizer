from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase
from django.urls import reverse

from .services import (
    CandidateStation,
    Coordinate,
    choose_fuel_stops,
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
            self._candidate("slightly_more_near", 260.0, 3.20, 1.0),
        ]

        plan = choose_fuel_stops(400.0, candidates, corridor_radius_miles=50.0)

        self.assertEqual(plan["selected_fuel_stops"][0]["truckstop_name"], "slightly_more_near")
        self.assertGreater(plan["selected_fuel_stops"][0]["detour_cost_usd"], 0)

    def test_divides_route_into_500_mile_windows(self) -> None:
        candidates = [
            self._candidate("first_window", 250.0, 3.10, 1.0),
            self._candidate("second_window", 650.0, 3.25, 1.0),
        ]

        plan = choose_fuel_stops(760.0, candidates, corridor_radius_miles=10.0)

        self.assertEqual(len(plan["selected_fuel_stops"]), 2)
        self.assertEqual(plan["selected_fuel_stops"][0]["window_end_mile"], 500.0)
        self.assertEqual(plan["selected_fuel_stops"][1]["window_end_mile"], 760.0)

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
        self.assertIn("error", response.json())
