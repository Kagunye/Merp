"""Notifications context processor."""


def notifications_context(request):
    if not request.user.is_authenticated:
        return {"unread_notifications_count": 0, "recent_notifications": []}

    try:
        from .models import Notification
        unread_count = Notification.objects.filter(
            user=request.user, is_read=False
        ).count()
        recent = Notification.objects.filter(
            user=request.user
        ).order_by("-created_at")[:5]
        return {
            "unread_notifications_count": unread_count,
            "recent_notifications": recent,
        }
    except Exception:
        return {"unread_notifications_count": 0, "recent_notifications": []}
