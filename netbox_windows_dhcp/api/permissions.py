from rest_framework.exceptions import APIException
from rest_framework.permissions import BasePermission


class _ServiceUnavailable(APIException):
    status_code = 503
    default_detail = 'Service temporarily unavailable.'
    default_code = 'service_unavailable'


class DHCPAPIEnabled(BasePermission):
    """Blocks all requests when DHCPPluginSettings.api_enabled is False."""

    def has_permission(self, request, view):
        from ..models import DHCPPluginSettings
        if not DHCPPluginSettings.load().api_enabled:
            raise _ServiceUnavailable(
                detail='The Windows DHCP plugin API is currently disabled.'
            )
        return True
