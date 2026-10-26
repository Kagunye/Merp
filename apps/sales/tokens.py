"""One-time form tickets: the same form sent twice books one document.

A form is printed with a fresh ticket (`issue`), and the view that takes the
post spends it (`spend`) before it writes anything. A ticket can be spent once.
The second copy of a double-clicked form arrives with a ticket that is already
spent and is turned away with a message, instead of booking a second receipt
or a second credit note that are each, on their own, perfectly valid.

The ticket is optional: a post that carries none is handled exactly as before,
so scripts and older screens that post straight to the view keep working. A
post that does carry one is always checked — a ticket that is unknown, spent,
for another form, another company or another user is refused.
"""
import secrets

from django.utils import timezone

FIELD = "form_token"


def issue(request, purpose):
    """A new unspent ticket for this user, company and form."""
    from .models import SubmissionToken
    company = getattr(request, "active_company", None)
    if company is None:
        return ""
    user = request.user if request.user.is_authenticated else None
    token = secrets.token_urlsafe(32)
    SubmissionToken.objects.create(token=token, purpose=purpose, company=company, user=user)
    return token


def spend(request, purpose):
    """Spend the ticket the post carries. True when the post may go ahead.

    No ticket at all is allowed through (see the module note). A ticket is
    spent with one conditional UPDATE, so of two copies of the same form sent
    at the same moment exactly one gets True.
    """
    from .models import SubmissionToken
    token = (request.POST.get(FIELD) or "").strip()
    if not token:
        return True
    company = getattr(request, "active_company", None)
    user = request.user if request.user.is_authenticated else None
    spent = SubmissionToken.objects.filter(
        token=token, purpose=purpose, company=company, user=user,
        used_at__isnull=True,
    ).update(used_at=timezone.now())
    return spent == 1


ALREADY_SENT = (
    "This form was already sent once, so the second copy was not recorded — "
    "a double click or a page refresh posts the same form again. Check the "
    "list below; if the document you meant is not there, open the form again "
    "and send it once.")
