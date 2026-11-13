"""The scheduled eTIMS run: pull KRA's lists and send the queue for every
company with eTIMS on. Call it from Vercel Cron (or any scheduler) with the
token in KRA_CRON_TOKEN, e.g. GET /kra/cron/?token=... every 5–15 minutes
(KRA's TIS spec expects data pushed at least every 15 minutes)."""
import hmac
import os

from django.http import HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
def run(request):
    expected = os.environ.get("KRA_CRON_TOKEN", "")
    given = request.GET.get("token") or request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not expected or not hmac.compare_digest(expected, given or ""):
        return HttpResponseForbidden("no")
    from apps.core import tenancy
    from apps.organizations.models import Company
    from . import services
    from .models import EtimsSettings
    done = {}
    # Company by company, each in its own database when it keeps one: the
    # shared database alone would never see those companies' queues.
    for company in Company.objects.filter(is_active=True):
        try:
            with tenancy.company_db(company):
                if not EtimsSettings.objects.filter(company_id=company.pk, enabled=True).exists():
                    continue
                done[str(company.pk)] = services.sync_all(company)
        except Exception as exc:   # noqa: BLE001 - one company must not stop the rest
            done[str(company.pk)] = f"error: {exc}"
    return JsonResponse({"ok": True, "companies": len(done), "result": done})
