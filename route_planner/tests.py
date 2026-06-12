from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from route_planner.management.commands.import_fuel_stations import (
    NOMINATIM_NO_RESULT_SOURCE,
    geocode_missing_stations,
)
from route_planner.models import FuelStation

from .services import (
    CandidateStation,
    Coordinate,
    choose_fuel_stops,
    fuel_leg_cost,
    plan_route,
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
    def test_short_route_response_is_assignment_focused(self) -> None:
        with (
            patch(
                "route_planner.services.geocode_route_endpoints",
                return_value=(Coordinate(40.0, -75.0), Coordinate(41.0, -74.0)),
            ),
            patch(
                "route_planner.services.fetch_route",
                return_value={
                    "distance_miles": 120.0,
                    "duration_minutes": 150.0,
                    "coordinates": [
                        Coordinate(40.0, -75.0),
                        Coordinate(41.0, -74.0),
                    ],
                },
            ),
            patch(
                "route_planner.services.StationIndex.from_database",
                side_effect=AssertionError("FuelStation table should not be loaded"),
            ),
        ):
            result = plan_route("Start, PA", "Finish, NY")

        self.assertEqual(result["fuel_plan"]["fuel_stops"], [])
        self.assertEqual(result["fuel_plan"]["total_route_gallons"], 12.0)
        self.assertEqual(result["fuel_plan"]["vehicle_range_miles"], 500.0)
        self.assertEqual(result["fuel_plan"]["fuel_efficiency_mpg"], 10.0)
        self.assertTrue(result["fuel_plan"]["starts_with_full_tank"])
        self.assertNotIn("station_search", result)
        self.assertNotIn("providers", result)
        self.assertNotIn("elapsed_ms", result)
        self.assertNotIn("map", result)
        self.assertNotIn("bounds", result["route"])

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


class ImportFuelStationsCommandTests(TestCase):
    def test_skips_existing_geocoded_station_without_nominatim_call(self) -> None:
        FuelStation.objects.create(
            opis_truckstop_id="geocoded-1",
            truckstop_name="Already Geocoded",
            address="I-80",
            city="Somewhere",
            state="PA",
            rack_id="1",
            retail_price=Decimal("3.1000"),
            latitude=Decimal("40.1234567"),
            longitude=Decimal("-75.1234567"),
            geocode_source="nominatim",
        )
        stdout = CaptureStdout()

        with patch(
            "route_planner.management.commands.import_fuel_stations._get_json",
            side_effect=AssertionError("geocoded rows should be skipped"),
        ):
            geocoded = geocode_missing_stations(
                stdout=stdout,
                success_style=str,
                warning_style=str,
            )

        self.assertEqual(geocoded, 0)
        self.assertTrue(
            any("Skipped 1 already geocoded stations." in line for line in stdout.lines)
        )

    @override_settings(GEOCODIO_API_KEYS=[])
    def test_uses_fallback_query_after_nominatim_no_result(self) -> None:
        station = FuelStation.objects.create(
            opis_truckstop_id="fallback-1",
            truckstop_name="Pilot Travel Center 123",
            address="Unknown Exit",
            city="Somewhere",
            state="PA",
            rack_id="1",
            retail_price=Decimal("3.1000"),
        )
        stdout = CaptureStdout()

        with (
            patch(
                "route_planner.management.commands.import_fuel_stations._get_json",
                side_effect=[
                    [],
                    [{"lat": "40.12345678", "lon": "-75.12345678"}],
                ],
            ) as get_json,
            patch("route_planner.management.commands.import_fuel_stations.time.sleep"),
        ):
            geocoded = geocode_missing_stations(
                stdout=stdout,
                success_style=str,
                warning_style=str,
            )

        station.refresh_from_db()
        self.assertEqual(geocoded, 1)
        self.assertEqual(station.latitude, Decimal("40.1234568"))
        self.assertEqual(station.longitude, Decimal("-75.1234568"))
        self.assertEqual(station.geocode_source, NOMINATIM_FALLBACK_SOURCE)
        self.assertEqual(get_json.call_count, 2)

    @override_settings(GEOCODIO_API_KEYS=[])
    def test_retries_legacy_nominatim_no_result_with_fallback(self) -> None:
        station = FuelStation.objects.create(
            opis_truckstop_id="legacy-missing-1",
            truckstop_name="Pilot Travel Center 456",
            address="Unknown Exit",
            city="Somewhere",
            state="PA",
            rack_id="1",
            retail_price=Decimal("3.1000"),
            geocode_source=NOMINATIM_NO_RESULT_SOURCE,
        )
        stdout = CaptureStdout()

        with (
            patch(
                "route_planner.management.commands.import_fuel_stations._get_json",
                side_effect=[
                    [],
                    [{"lat": "40.12345678", "lon": "-75.12345678"}],
                ],
            ),
            patch("route_planner.management.commands.import_fuel_stations.time.sleep"),
        ):
            geocoded = geocode_missing_stations(
                stdout=stdout,
                success_style=str,
                warning_style=str,
            )

        station.refresh_from_db()
        self.assertEqual(geocoded, 1)
        self.assertEqual(station.geocode_source, NOMINATIM_FALLBACK_SOURCE)

    @override_settings(GEOCODIO_API_KEYS=["key-a", "key-b"])
    def test_uses_geocodio_before_nominatim(self) -> None:
        station = FuelStation.objects.create(
            opis_truckstop_id="geocodio-1",
            truckstop_name="Precise Station",
            address="1109 N Highland St",
            city="Arlington",
            state="VA",
            rack_id="1",
            retail_price=Decimal("3.1000"),
        )
        stdout = CaptureStdout()

        with (
            patch(
                "route_planner.management.commands.import_fuel_stations._get_json",
                side_effect=[
                    {
                        "results": [
                            {
                                "accuracy": 1,
                                "accuracy_type": "rooftop",
                                "location": {
                                    "lat": 38.886665,
                                    "lng": -77.094733,
                                },
                            },
                        ],
                    },
                ],
            ) as get_json,
            patch("route_planner.management.commands.import_fuel_stations.time.sleep"),
        ):
            geocoded = geocode_missing_stations(
                stdout=stdout,
                success_style=str,
                warning_style=str,
            )

        station.refresh_from_db()
        self.assertEqual(geocoded, 1)
        self.assertEqual(station.latitude, Decimal("38.8866650"))
        self.assertEqual(station.longitude, Decimal("-77.0947330"))
        self.assertEqual(station.geocode_source, GEOCODIO_SOURCE)
        self.assertEqual(get_json.call_count, 1)
        self.assertIn("api.geocod.io/v2/geocode", get_json.call_args_list[0].args[0])

    @override_settings(GEOCODIO_API_KEYS=["key-a"])
    def test_accepts_highway_exit_geocodio_street_center_result(self) -> None:
        station = FuelStation.objects.create(
            opis_truckstop_id="geocodio-highway-exit",
            truckstop_name="TA BINGHAMTON TRAVELCENTER",
            address="I-81N, EXIT 2W & I-81S, EXIT 3",
            city="Binghamton",
            state="NY",
            rack_id="32",
            retail_price=Decimal("3.6890"),
        )
        stdout = CaptureStdout()

        with (
            patch(
                "route_planner.management.commands.import_fuel_stations._get_json",
                side_effect=[
                    {
                        "results": [
                            {
                                "accuracy": 0.78,
                                "accuracy_type": "street_center",
                                "location": {
                                    "lat": 42.102705,
                                    "lng": -75.827683,
                                },
                            },
                        ],
                    },
                ],
            ),
            patch("route_planner.management.commands.import_fuel_stations.time.sleep"),
        ):
            geocoded = geocode_missing_stations(
                stdout=stdout,
                success_style=str,
                warning_style=str,
            )

        station.refresh_from_db()
        self.assertEqual(geocoded, 1)
        self.assertEqual(station.latitude, Decimal("42.1027050"))
        self.assertEqual(station.longitude, Decimal("-75.8276830"))
        self.assertEqual(station.geocode_source, GEOCODIO_SOURCE)

    @override_settings(GEOCODIO_API_KEYS=["key-a"])
    def test_rejects_near_threshold_street_center_for_non_exit_address(self) -> None:
        station = FuelStation.objects.create(
            opis_truckstop_id="geocodio-street-center-low",
            truckstop_name="Low Accuracy Station",
            address="Unknown Road",
            city="Binghamton",
            state="NY",
            rack_id="32",
            retail_price=Decimal("3.6890"),
        )
        stdout = CaptureStdout()

        with (
            patch(
                "route_planner.management.commands.import_fuel_stations._get_json",
                side_effect=[
                    {
                        "results": [
                            {
                                "accuracy": 0.78,
                                "accuracy_type": "street_center",
                                "location": {
                                    "lat": 42.102705,
                                    "lng": -75.827683,
                                },
                            },
                        ],
                    },
                    [],
                    [],
                ],
            ),
            patch("route_planner.management.commands.import_fuel_stations.time.sleep"),
        ):
            geocoded = geocode_missing_stations(
                stdout=stdout,
                success_style=str,
                warning_style=str,
            )

        station.refresh_from_db()
        self.assertEqual(geocoded, 0)
        self.assertIsNone(station.latitude)
        self.assertIsNone(station.longitude)
        self.assertEqual(station.geocode_source, NOMINATIM_FALLBACK_NO_RESULT_SOURCE)

    @override_settings(GEOCODIO_API_KEYS=["key-a", "key-b"])
    def test_tries_second_geocodio_key_when_first_key_fails(self) -> None:
        station = FuelStation.objects.create(
            opis_truckstop_id="geocodio-2",
            truckstop_name="Precise Station",
            address="1109 N Highland St",
            city="Arlington",
            state="VA",
            rack_id="1",
            retail_price=Decimal("3.1000"),
        )
        stdout = CaptureStdout()

        with (
            patch(
                "route_planner.management.commands.import_fuel_stations._get_json",
                side_effect=[
                    UpstreamServiceError("first key is over quota"),
                    {
                        "results": [
                            {
                                "accuracy": 1,
                                "accuracy_type": "rooftop",
                                "location": {
                                    "lat": 38.886665,
                                    "lng": -77.094733,
                                },
                            },
                        ],
                    },
                ],
            ) as get_json,
            patch("route_planner.management.commands.import_fuel_stations.time.sleep"),
        ):
            geocoded = geocode_missing_stations(
                stdout=stdout,
                success_style=str,
                warning_style=str,
            )

        station.refresh_from_db()
        self.assertEqual(geocoded, 1)
        self.assertEqual(station.geocode_source, GEOCODIO_SOURCE)
        self.assertEqual(get_json.call_count, 2)

    @override_settings(GEOCODIO_API_KEYS=["key-a"])
    def test_rejects_low_accuracy_geocodio_result(self) -> None:
        station = FuelStation.objects.create(
            opis_truckstop_id="geocodio-low-accuracy",
            truckstop_name="Imprecise Station",
            address="Unknown Exit",
            city="Nowhere",
            state="PA",
            rack_id="1",
            retail_price=Decimal("3.1000"),
        )
        stdout = CaptureStdout()

        with (
            patch(
                "route_planner.management.commands.import_fuel_stations._get_json",
                side_effect=[
                    {
                        "results": [
                            {
                                "accuracy": 0.5,
                                "accuracy_type": "place",
                                "location": {
                                    "lat": 40.0,
                                    "lng": -75.0,
                                },
                            },
                        ],
                    },
                    [],
                    [],
                ],
            ),
            patch("route_planner.management.commands.import_fuel_stations.time.sleep"),
        ):
            geocoded = geocode_missing_stations(
                stdout=stdout,
                success_style=str,
                warning_style=str,
            )

        station.refresh_from_db()
        self.assertEqual(geocoded, 0)
        self.assertIsNone(station.latitude)
        self.assertIsNone(station.longitude)
        self.assertEqual(station.geocode_source, NOMINATIM_FALLBACK_NO_RESULT_SOURCE)

    def test_skips_station_after_all_geocoding_queries_have_no_result(self) -> None:
        station = FuelStation.objects.create(
            opis_truckstop_id="missing-1",
            truckstop_name="Missing Station",
            address="Unknown Exit",
            city="Nowhere",
            state="PA",
            rack_id="1",
            retail_price=Decimal("3.1000"),
        )
        stdout = CaptureStdout()

        with (
            patch(
                "route_planner.management.commands.import_fuel_stations._get_json",
                return_value=[],
            ) as get_json,
            patch("route_planner.management.commands.import_fuel_stations.time.sleep"),
        ):
            geocoded = geocode_missing_stations(
                stdout=stdout,
                success_style=str,
                warning_style=str,
            )

        station.refresh_from_db()
        self.assertEqual(geocoded, 0)
        self.assertEqual(station.geocode_source, NOMINATIM_FALLBACK_NO_RESULT_SOURCE)
        self.assertEqual(get_json.call_count, 3)

        with (
            patch(
                "route_planner.management.commands.import_fuel_stations._get_json",
                return_value=[],
            ) as second_get_json,
            patch("route_planner.management.commands.import_fuel_stations.time.sleep"),
        ):
            geocoded = geocode_missing_stations(
                stdout=stdout,
                success_style=str,
                warning_style=str,
            )

        self.assertEqual(geocoded, 0)
        self.assertEqual(second_get_json.call_count, 3)

    @override_settings(GEOCODIO_API_KEYS=[])
    def test_reprocesses_fallback_no_result_station_by_default(self) -> None:
        station = FuelStation.objects.create(
            opis_truckstop_id="retry-missing-1",
            truckstop_name="Retry Station",
            address="1109 N Highland St",
            city="Arlington",
            state="VA",
            rack_id="1",
            retail_price=Decimal("3.1000"),
            geocode_source=NOMINATIM_FALLBACK_NO_RESULT_SOURCE,
        )
        stdout = CaptureStdout()

        with (
            patch(
                "route_planner.management.commands.import_fuel_stations._get_json",
                side_effect=[
                    [{"lat": "38.886665", "lon": "-77.094733"}],
                ],
            ) as get_json,
            patch("route_planner.management.commands.import_fuel_stations.time.sleep"),
        ):
            geocoded = geocode_missing_stations(
                stdout=stdout,
                success_style=str,
                warning_style=str,
            )

        station.refresh_from_db()
        self.assertEqual(geocoded, 1)
        self.assertEqual(station.latitude, Decimal("38.8866650"))
        self.assertEqual(station.longitude, Decimal("-77.0947330"))
        self.assertEqual(station.geocode_source, "nominatim")
        self.assertEqual(get_json.call_count, 1)


class CaptureStdout:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, value: str) -> None:
        self.lines.append(value)
