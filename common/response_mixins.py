"""All views will extend this BaseAPIView View."""

from django.conf import settings
from rest_framework import status

from rest_framework.response import Response
from rest_framework.status import is_server_error
from rest_framework.views import APIView


class BaseAPIView(APIView):
    """Base class for API views."""

    @staticmethod
    def make_response_body(
        success=None,
        status_code=None,
        message=None,
        data=None,
    ):
        """Make standard response body
        :param data
        :param success
        :param message
        :param status_code
        :return : dictionary including all above params
        """
        return {
            "success": {} if success is None else success,
            "status_code": {} if status_code is None else status_code,
            "message": {} if message is None else message,
            "data": {} if data is None else data,
        }

    def send_response(
        self,
        success=None,
        status_code=None,
        message=None,
        data=None,
    ):
        """
        Generates response.
        :param data:dict  data generated for respective API call.
        :param meta: dict meta, a status code and message.
        :param success
        :param status_code
        :param message
        :rtype: dict.
        """
        if is_server_error(status_code):
            if settings.DEBUG:
                message = f"error message: {message}"
            else:
                message = "Internal server error."
        return Response(
            data=self.make_response_body(
                success=success, status_code=status_code, message=message, data=data
            ),
            status=status_code,
        )

    def send_success_response(self, message=None, data=None, **kwargs):
        """compose success response"""
        status_code = status.HTTP_200_OK
        message = message
        success = True
        return self.send_response(
            success=success,
            status_code=status_code,
            message=message,
            data=data,
        )

    def send_bad_request_response(self, message, **kwargs):
        """Compose failed request response"""
        status_code = status.HTTP_400_BAD_REQUEST
        message = message
        success = False

        return self.send_response(
            success=success, status_code=status_code, message=message, data=None
        )