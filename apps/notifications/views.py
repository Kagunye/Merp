from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Notification


@login_required
def notification_list(request):
    notifications = Notification.objects.filter(user=request.user).order_by("-created_at")[:50]
    return render(request, "notifications/list.html", {
        "notifications": notifications,
        "page_title": "Notifications",
        "has_unread": any(not n.is_read for n in notifications),
    })


def _done(request):
    """Answer a script with JSON and a person with the list they came from.

    Marking read changes state, so it is a POST: a GET is something a browser
    may prefetch, a link preview may follow, or an image tag on any page may
    fire, and none of those should clear somebody's unread notices.
    """
    if (request.headers.get("HX-Request")
            or request.headers.get("Accept", "").startswith("application/json")
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"):
        return JsonResponse({"status": "ok"})
    return redirect("notifications:list")


@login_required
@require_POST
def mark_read(request, pk):
    n = get_object_or_404(Notification, pk=pk, user=request.user)
    n.mark_read()
    return _done(request)


@login_required
@require_POST
def mark_all_read(request):
    from django.utils import timezone
    Notification.objects.filter(user=request.user, is_read=False).update(
        is_read=True, read_at=timezone.now())
    return _done(request)
