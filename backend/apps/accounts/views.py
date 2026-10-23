from django.contrib.auth import authenticate
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.audit.services import audit

from .permissions import role_of


class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        username = str(request.data.get("username", ""))
        password = str(request.data.get("password", ""))
        user = authenticate(request, username=username, password=password)
        if user is None or not user.is_active:
            audit(request, "login_failed", target=username[:100])
            return Response({"detail": "invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)
        if role_of(user) is None:
            audit(request, "login_denied_no_role", target=username[:100])
            return Response({"detail": "user has no role assigned"}, status=status.HTTP_403_FORBIDDEN)
        token, _ = Token.objects.get_or_create(user=user)
        request.user = user
        audit(request, "login", target=username)
        return Response({"token": token.key, "username": user.username, "role": role_of(user)})


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        audit(request, "logout")
        return Response({"detail": "logged out"})


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"username": request.user.username, "role": role_of(request.user)})
