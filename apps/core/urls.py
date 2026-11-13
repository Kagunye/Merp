from django.shortcuts import redirect
from django.urls import path

from . import offline


def _root(request):
    """Send unauthenticated visitors to the login page; signed-in users
    jump straight to their dashboard."""
    if request.user.is_authenticated:
        return redirect("dashboard:index")
    return redirect("accounts:login")


urlpatterns = [
    path("", _root, name="home"),
    # Offline mode (apps/core/offline.py)
    path("sw.js", offline.service_worker, name="sw"),
    path("manifest.webmanifest", offline.manifest, name="webmanifest"),
    path("offline/", offline.offline_page, name="offline"),
    path("ping/", offline.ping, name="ping"),
]
