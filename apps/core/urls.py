from django.shortcuts import redirect
from django.urls import path


def _root(request):
    """Send unauthenticated visitors to the login page; signed-in users
    jump straight to their dashboard."""
    if request.user.is_authenticated:
        return redirect("dashboard:index")
    return redirect("accounts:login")


urlpatterns = [
    path("", _root, name="home"),
]
