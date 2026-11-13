"""Offline mode: the server half.

The browser half (static/sw.js and static/js/offline.js) keeps visited pages
and the app's files for use without a connection, and saves chosen forms and
till tickets on the device while offline. When the connection is back it
replays each one with two headers:

    X-Offline-Sync: 1            "answer in JSON, I am a background sync"
    X-Offline-Key: <device id>   "this is the item I mean"

OfflineSyncMiddleware runs the normal view (with every normal check: login,
company, role, CSRF, the form's own validation) and turns its answer into
JSON the device understands: a redirect means it was saved; a page that came
back is a form with errors, kept on the device for the user to fix. The key
makes a replay idempotent: the first answer is stored and handed back again,
so a sync whose reply was lost never saves the same thing twice.
"""
import json
import re

from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache

KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _messages(request):
    from django.contrib.messages import get_messages
    return [str(m) for m in get_messages(request)]


def _form_errors(response):
    ctx = getattr(response, "context_data", None) or {}
    form = ctx.get("form") if isinstance(ctx, dict) else None
    out = []
    if form is not None and getattr(form, "errors", None):
        for field, errs in form.errors.items():
            label = "" if field == "__all__" else (form.fields[field].label or field) + ": " if field in form.fields else ""
            out += [f"{label}{e}" for e in errs]
    return out


class OfflineSyncMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method != "POST" or request.headers.get("X-Offline-Sync") != "1":
            return self.get_response(request)
        key = request.headers.get("X-Offline-Key", "")
        if not KEY.match(key):
            return JsonResponse({"ok": False, "permanent": True,
                                 "error": "This saved item has no valid id; it cannot be synced safely."},
                                status=400)
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return JsonResponse({"ok": False, "permanent": False, "signin": True,
                                 "error": "Sign in again, then sync."}, status=401)
        from .models import OfflineSubmission
        done = OfflineSubmission.objects.filter(key=key).first()
        if done is not None:
            if done.user_id != user.pk:
                return JsonResponse({"ok": False, "permanent": True,
                                     "error": "This saved item belongs to someone else."}, status=403)
            return JsonResponse({**done.result, "replayed": True})

        response = self.get_response(request)
        result = self.translate(request, response)
        if result.get("ok") or result.get("permanent"):
            OfflineSubmission.objects.get_or_create(
                key=key, defaults={"user": user, "path": request.path[:255], "result": result})
        return JsonResponse(result, status=200 if result.get("ok") else 422 if result.get("permanent") else 503)

    @staticmethod
    def translate(request, response):
        """What the view's answer means for a saved item."""
        ctype = response.get("Content-Type", "")
        if ctype.startswith("application/json"):
            try:
                data = json.loads(response.content or b"{}")
            except ValueError:
                data = {}
            if response.status_code < 400:
                return {"ok": data.get("ok", True), **data}
            return {"ok": False, "permanent": response.status_code < 500, **data}
        if response.status_code in (301, 302, 303):
            location = response.get("Location", "")
            if "/auth/login" in location:
                return {"ok": False, "permanent": False, "signin": True, "error": "Sign in again, then sync."}
            return {"ok": True, "location": location, "messages": _messages(request)}
        if response.status_code == 403:
            return {"ok": False, "permanent": True,
                    "error": "Your role may not do this, or the page expired (CSRF). Open the form and save it again."}
        if response.status_code >= 500:
            return {"ok": False, "permanent": False, "error": f"The server had a problem (HTTP {response.status_code}); it will be retried."}
        if response.status_code == 404:
            return {"ok": False, "permanent": True, "error": "What this was saved against no longer exists."}
        errors = _form_errors(response) or _messages(request)
        return {"ok": False, "permanent": True,
                "error": "; ".join(errors)[:500] or "The form came back with errors. Open it again to fix them."}


# ───────────────────────────── the offline endpoints ────────────────────────────

def service_worker(request):
    """Served from the site root so it can look after every page."""
    from django.conf import settings
    from django.contrib.staticfiles import finders
    path = finders.find("sw.js") or str(settings.BASE_DIR / "static" / "sw.js")
    response = FileResponse(open(path, "rb"), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


def manifest(request):
    from django.templatetags.static import static
    data = {
        "name": "Mofisi Business Suite", "short_name": "Mofisi", "start_url": "/dashboard/",
        "scope": "/", "display": "standalone", "background_color": "#f6f4f2", "theme_color": "#1a1416",
        "description": "Accounting, sales, stock, payroll and POS — works offline and syncs when back online.",
        "icons": [{"src": static("merp/images/mofisi-mark.png"), "sizes": "512x512", "type": "image/png",
                   "purpose": "any maskable"},
                  {"src": static("merp/images/favicon.png"), "sizes": "192x192", "type": "image/png"}],
    }
    return HttpResponse(json.dumps(data), content_type="application/manifest+json")


def offline_page(request):
    return render(request, "offline.html", {})


@never_cache
def ping(request):
    """Is the server reachable right now? (The till asks before ringing.)"""
    return HttpResponse("ok", content_type="text/plain")
