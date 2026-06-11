from __future__ import annotations

import json

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .services import BadRequest, RoutePlannerError, plan_route


@csrf_exempt
@require_http_methods(["GET", "POST"])
def route_plan_view(request: HttpRequest) -> JsonResponse:
    try:
        payload = _request_payload(request)
        result = plan_route(payload["start"], payload["finish"])
    except RoutePlannerError as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status_code)

    return JsonResponse(result, json_dumps_params={"separators": (",", ":")})


@require_http_methods(["GET"])
def health_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


def _request_payload(request: HttpRequest) -> dict[str, str]:
    if request.method == "GET":
        start = request.GET.get("start_location", "") or request.GET.get("start", "")
        finish = (
            request.GET.get("finish_location", "")
            or request.GET.get("finish", "")
            or request.GET.get("end", "")
        )
    else:
        try:
            raw_payload = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise BadRequest("Request body must be valid JSON.") from exc

        start = raw_payload.get("start_location", "") or raw_payload.get("start", "")
        finish = (
            raw_payload.get("finish_location", "")
            or raw_payload.get("finish", "")
            or raw_payload.get("end", "")
            or raw_payload.get("destination", "")
        )

    start = " ".join(str(start or "").split())
    finish = " ".join(str(finish or "").split())
    if not start or not finish:
        raise BadRequest("Both start_location and finish_location are required.")

    return {"start": start, "finish": finish}
