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


class ScopeInfoWritable(BasePermission):
    """
    While DHCPPluginSettings.push_scope_info is off — the DHCP server is the source of
    truth then — refuses creating scopes, exclusion ranges and option values, and editing
    option values. Reads and deletes are unaffected (a delete never reaches the server,
    and the next sync imports again anything still on it). Scope and exclusion edits go
    through; their serializers refuse changing any field the server owns
    (view.push_off_editable).
    """

    def has_permission(self, request, view):
        from rest_framework.exceptions import PermissionDenied
        from rest_framework.permissions import SAFE_METHODS

        from ..models import DHCPPluginSettings
        if request.method in SAFE_METHODS or request.method == 'DELETE':
            return True
        if request.method in ('PUT', 'PATCH') and getattr(view, 'push_off_editable', False):
            return True
        if DHCPPluginSettings.load().push_scope_info:
            return True
        name = view.queryset.model._meta.verbose_name_plural
        raise PermissionDenied(
            detail=f'{name} are read-only while "Push Scope Info" is disabled. '
                   f'Enable it in plugin settings to manage them from NetBox.'
        )


class LeaseInfoReadable(BasePermission):
    """
    Lease info is read-only and has no permissions of its own: any signed-in user (or
    anyone, when LOGIN_REQUIRED is off) may ask, and the viewset lists only entries whose
    IP Address the user may view.
    """

    def has_permission(self, request, view):
        from django.conf import settings
        from rest_framework.permissions import SAFE_METHODS

        if request.method not in SAFE_METHODS:
            return False
        return request.user.is_authenticated or not settings.LOGIN_REQUIRED
