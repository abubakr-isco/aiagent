"""Role-based access: viewer < operator < admin (Django groups)."""

from rest_framework.permissions import SAFE_METHODS, BasePermission

VIEWER, OPERATOR, ADMIN = "viewer", "operator", "admin"
ROLES = (VIEWER, OPERATOR, ADMIN)


def role_of(user) -> str | None:
    if not user or not user.is_authenticated:
        return None
    if user.is_superuser:
        return ADMIN
    names = set(user.groups.values_list("name", flat=True))
    for role in (ADMIN, OPERATOR, VIEWER):
        if role in names:
            return role
    return None


def has_role(user, minimum: str) -> bool:
    role = role_of(user)
    return role is not None and ROLES.index(role) >= ROLES.index(minimum)


class IsViewer(BasePermission):
    def has_permission(self, request, view):
        return has_role(request.user, VIEWER)


class IsOperator(BasePermission):
    """Start/pause/stop/emergency stop."""

    def has_permission(self, request, view):
        return has_role(request.user, OPERATOR)


class IsAdmin(BasePermission):
    """Settings, mode changes (incl. LIVE), user-level actions."""

    def has_permission(self, request, view):
        return has_role(request.user, ADMIN)


class ReadViewerWriteAdmin(BasePermission):
    def has_permission(self, request, view):
        return has_role(request.user, VIEWER if request.method in SAFE_METHODS else ADMIN)
