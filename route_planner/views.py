from __future__ import annotations

from rest_framework import status
from rest_framework.generics import CreateAPIView
from rest_framework.request import Request

from common.response_mixins import BaseAPIView
from .serializers import RoutePlanRequestSerializer
from .services import RoutePlannerError


class RoutePlanCreateAPIView(BaseAPIView, CreateAPIView):
    serializer_class = RoutePlanRequestSerializer

    def create(self, request: Request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return self.send_bad_request_response(message=serializer.errors)

        try:
            result = serializer.save()
        except RoutePlannerError as exc:
            return self.send_response(
                success=False,
                status_code=getattr(exc, "status_code", status.HTTP_500_INTERNAL_SERVER_ERROR),
                message=str(exc),
                data=None,
            )

        return self.send_success_response(
            message="Route plan created successfully",
            data=result,
        )
