from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from . import render as r
from . import services
from .forms import TagBatchForm, split_names
from .models import SIZES, Tag, TagBatch


def _company(request):
    company = request.active_company
    if company is None:
        raise Http404("Choose a company first.")
    return company


def _int(value, default=None):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _logo(company):
    from apps.organizations.documents import layout_for, logo_url
    return logo_url(layout_for(company))


def _price(batch, company):
    if not (batch.show_price and batch.purpose == "PRODUCT" and batch.product_id):
        return ""
    from apps.core.templatetags.ui_extras import money
    return f"{company.currency} {money(batch.product.selling_price)}"


def _labels(batch, company, tags, copies=1):
    price = _price(batch, company)
    out = []
    for tag in tags:
        label = {"tag": tag, "price": price,
                 "qr": r.qr_svg(r.payload(tag, batch, company), f"QR code {tag.code}") if batch.show_qr else "",
                 "barcode": r.code128_svg(tag.code) if batch.show_barcode else ""}
        out.extend([label] * copies)
    return out


@login_required
def home(request):
    company = _company(request)
    batches = (TagBatch.objects.filter(company=company)
               .annotate(void_count=Count("tags", filter=Q(tags__status="VOID")))
               .select_related("created_by").order_by("-created_at"))
    page = Paginator(batches, 30).get_page(request.GET.get("page"))
    totals = Tag.objects.filter(company=company).aggregate(
        all=Count("id"), void=Count("id", filter=Q(status="VOID")))
    return render(request, "tagging/home.html", {"page": page, "totals": totals})


@login_required
def lookup(request):
    company = _company(request)
    q = (request.GET.get("q") or "").strip()
    code = r.code_from_scan(q)
    results = []
    if code:
        found = Tag.objects.filter(company=company, code__iexact=code).first()
        if found is None:
            named = list(Tag.objects.filter(company=company, name__iexact=code)[:2])
            found = named[0] if len(named) == 1 else None
        if found:
            return redirect("tagging:tag_detail", found.pk)
        results = (Tag.objects.filter(company=company)
                   .filter(Q(code__icontains=code) | Q(name__icontains=code))
                   .select_related("batch").order_by("code")[:25])
    return render(request, "tagging/lookup.html", {"q": q, "code": code, "results": results})


@login_required
def batch_create(request):
    company = _company(request)
    initial = {"prefix": request.GET.get("prefix", "TAG-")}
    if request.GET.get("product"):
        initial.update(purpose="PRODUCT", product=request.GET["product"])
    if request.GET.get("asset"):
        initial.update(purpose="ASSET", asset=request.GET["asset"])
    form = TagBatchForm(request.POST or None, company=company, initial=initial)
    if request.method == "POST" and form.is_valid():
        batch = form.save(commit=False)
        batch.company = company
        try:
            services.issue_batch(batch, start=form.cleaned_data.get("start"), user=request.user,
                                 names=form.cleaned_data.get("names"))
        except ValidationError as e:
            form.add_error("start", e.messages[0])
        else:
            messages.success(request, f"{batch.number}: {batch.quantity} tags issued, "
                                      f"{batch.first_code} to {batch.last_code}.")
            return redirect("tagging:batch_detail", batch.pk)
    prefix = (form.data.get("prefix") if form.is_bound else initial["prefix"]) or "TAG-"
    prefix = prefix.strip().upper()
    return render(request, "tagging/batch_form.html", {
        "form": form, "next_free": services.next_free(company, prefix), "prefix": prefix,
        "sizes": SIZES,
    })


@login_required
def batch_detail(request, pk):
    company = _company(request)
    batch = get_object_or_404(TagBatch.objects.select_related("product", "asset", "created_by"),
                              pk=pk, company=company)
    tags = batch.tags.select_related("voided_by")
    status = request.GET.get("status")
    if status in ("ACTIVE", "VOID"):
        tags = tags.filter(status=status)
    page = Paginator(tags, 100).get_page(request.GET.get("page"))
    first = batch.tags.filter(status="ACTIVE").first() or batch.tags.first()
    return render(request, "tagging/batch_detail.html", {
        "batch": batch, "page": page, "status": status,
        "preview": _labels(batch, company, [first])[0] if first else None,
        "logo": _logo(company) if batch.show_logo else "", "company": company,
        "counts": batch.tags.aggregate(active=Count("id", filter=Q(status="ACTIVE")),
                                       void=Count("id", filter=Q(status="VOID"))),
        "sizes": SIZES, "max_print": services.MAX_PRINT,
    })


def _print_selection(request, batch):
    first = _int(request.GET.get("from"), batch.start_number)
    last = _int(request.GET.get("to"), batch.end_number)
    if first > last:
        first, last = last, first
    copies = min(_int(request.GET.get("copies"), 1), 10)
    tags = list(services.tags_in_range(batch, first, last)[:services.MAX_PRINT + 1])
    return first, last, copies, tags


@login_required
def batch_print(request, pk):
    """Standalone print page: exact @page size, labels on a grid, black on
    white. Reprints use the batch's own codes; nothing new is numbered."""
    company = _company(request)
    batch = get_object_or_404(TagBatch.objects.select_related("product", "asset"), pk=pk, company=company)
    first, last, copies, tags = _print_selection(request, batch)
    too_many = len(tags) > services.MAX_PRINT
    tags = tags[:services.MAX_PRINT]
    size_key = request.GET.get("size") if request.GET.get("size") in SIZES else batch.label_size
    name, pw, ph, cols, rows, lw, lh, top, left, cgap, rgap = SIZES[size_key]
    per_page = cols * rows
    skip = min(_int(request.GET.get("skip"), 0) or 0, per_page - 1) if per_page > 1 else 0
    cells = [None] * skip + _labels(batch, company, tags, copies)
    pages = [cells[i:i + per_page] for i in range(0, len(cells), per_page)] if cells else []
    if tags and request.GET.get("preview") != "1":
        services.mark_printed(t.pk for t in tags)
    return render(request, "tagging/print.html", {
        "batch": batch, "company": company, "pages": pages, "count": len(tags), "copies": copies,
        "first": batch.code_for(first), "last": batch.code_for(last), "too_many": too_many,
        "max_print": services.MAX_PRINT, "logo": _logo(company) if batch.show_logo else "",
        "size": {"key": size_key, "name": name, "pw": pw, "ph": ph, "cols": cols, "rows": rows,
                 "lw": lw, "lh": lh, "top": top, "left": left, "cgap": cgap, "rgap": rgap,
                 "sheet": per_page > 1},
        "back": reverse("tagging:batch_detail", args=[batch.pk]),
    })


@login_required
def batch_zpl(request, pk):
    """The same labels as ZPL II for a Zebra-type printer (203 dpi)."""
    company = _company(request)
    batch = get_object_or_404(TagBatch.objects.select_related("product"), pk=pk, company=company)
    first, last, copies, tags = _print_selection(request, batch)
    tags = tags[:services.MAX_PRINT]
    body = r.zpl(tags, batch, company, copies=copies, price=_price(batch, company))
    resp = HttpResponse(body, content_type="text/plain; charset=utf-8")
    name = slugify(f"{batch.number}-{batch.code_for(first)}-{batch.code_for(last)}") or "tags"
    resp["Content-Disposition"] = f'attachment; filename="{name}.zpl"'
    if tags:
        services.mark_printed(t.pk for t in tags)
    return resp


@login_required
@require_POST
def batch_void(request, pk):
    company = _company(request)
    batch = get_object_or_404(TagBatch, pk=pk, company=company)
    first = _int(request.POST.get("from"))
    last = _int(request.POST.get("to"), first)
    reason = (request.POST.get("reason") or "").strip()
    back = request.POST.get("next") or reverse("tagging:batch_detail", args=[batch.pk])
    if not back.startswith("/"):
        back = reverse("tagging:batch_detail", args=[batch.pk])
    if not first:
        messages.error(request, "Give the number to void (and the last one, for a range).")
        return redirect(back)
    if not reason:
        messages.error(request, "Say why the tags are void (lost, damaged, misprinted...).")
        return redirect(back)
    n = services.void(batch, first, last, request.user, reason)
    if n:
        messages.success(request, f"{n} tag{'s' if n != 1 else ''} voided. Their numbers are not issued again.")
    else:
        messages.info(request, "No active tags in that range.")
    return redirect(back)


@login_required
@require_POST
def tag_rename(request, pk):
    company = _company(request)
    tag = get_object_or_404(Tag, pk=pk, company=company)
    tag.name = (request.POST.get("name") or "").strip()[:80]
    tag.save(update_fields=["name"])
    messages.success(request, f"{tag.code} is now named \"{tag.name}\"." if tag.name else f"{tag.code} has no name now.")
    return redirect("tagging:tag_detail", tag.pk)


@login_required
@require_POST
def batch_names(request, pk):
    """Name several tags at once: one name per line from a starting number."""
    company = _company(request)
    batch = get_object_or_404(TagBatch, pk=pk, company=company)
    first = _int(request.POST.get("from"), batch.start_number)
    names = split_names(request.POST.get("names"))
    if not names:
        messages.error(request, "Type the names, one per line.")
    else:
        n = services.name_tags(batch, first, names)
        messages.success(request, f"{n} tag{'s' if n != 1 else ''} named.")
    return redirect("tagging:batch_detail", batch.pk)


@login_required
def tag_detail(request, pk):
    company = _company(request)
    tag = get_object_or_404(Tag.objects.select_related("batch", "batch__product", "batch__asset", "voided_by"),
                            pk=pk, company=company)
    batch = tag.batch
    label = _labels(batch, company, [tag])[0]
    return render(request, "tagging/tag_detail.html", {
        "tag": tag, "batch": batch, "label": label, "company": company,
        "logo": _logo(company) if batch.show_logo else "",
        "payload": r.payload(tag, batch, company),
        "big_qr": r.qr_svg(r.payload(tag, batch, company), f"QR code {tag.code}") if batch.show_qr else "",
        "barcode": r.code128_svg(tag.code) if batch.show_barcode else "",
    })
