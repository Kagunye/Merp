"""KRA eTIMS template helpers.

    {% load kra_tags %}
    {% kra_block "sales.Invoice" invoice.pk %}            {# card on a document page #}
    {% kra_block "pos.POSSale" sale.pk compact=True %}    {# 80 mm thermal receipt #}
    {{ receipt.qr_url|qr_svg }}                            {# inline SVG QR code #}
    {{ receipt.signature|kra_dashed }}                     {# ABCD-EFGH-… #}
"""
import re

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def kra_dashed(value):
    from apps.kra.models import EtimsReceipt
    return EtimsReceipt.dashed(value)


@register.filter
def qr_svg(value, size=""):
    """An inline SVG QR code for `value`, black on a white square so it scans
    in dark mode too. `size` (pixels) fixes the width and height; without it
    the SVG fills its box (class "kra-qr")."""
    if not value:
        return ""
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(str(value), image_factory=qrcode.image.svg.SvgPathImage, border=2,
                      error_correction=qrcode.constants.ERROR_CORRECT_M)
    svg = img.to_string(encoding="unicode")
    svg = re.sub(r"^<\?xml[^>]*\?>\s*", "", svg)
    view = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    w, h = (view.group(1), view.group(2)) if view else ("100%", "100%")
    dims = f'width="{int(size)}" height="{int(size)}"' if str(size).isdigit() else 'class="kra-qr block h-full w-full"'
    svg = re.sub(r'\s(width|height)="[^"]*"', "", svg, count=2)
    svg = svg.replace("<svg ", f'<svg {dims} role="img" aria-label="KRA receipt QR code" '
                               'shape-rendering="crispEdges" ', 1)
    # A white backing square first, so the dark modules always have contrast.
    svg = re.sub(r"(<svg[^>]*>)", rf'\1<rect width="{w}" height="{h}" fill="#ffffff"/>', svg, count=1)
    return mark_safe(svg)


@register.inclusion_tag("kra/_receipt_block.html", takes_context=True)
def kra_block(context, source_type, source_id, compact=False):
    """What KRA signed for a document, or that it is still waiting. Draws
    nothing when eTIMS is off or the document was never queued."""
    out = {"request": context.get("request"), "compact": compact, "receipt": None, "sub": None}
    if not source_id:
        return out
    from apps.kra import services
    from apps.kra.models import EtimsReceipt

    try:
        receipt, sub = services.status_for(source_type, source_id)
    except Exception:       # noqa: BLE001 - a KRA problem never breaks the document page
        return out
    if receipt is None and (sub is None or sub.status == "CANCELLED" or not services.is_on(sub.company)):
        return out
    out.update(receipt=receipt, sub=sub)
    if receipt and receipt.receipt_type == "R" and receipt.original_invoice_no:
        original = EtimsReceipt.objects.filter(device_id=receipt.device_id,
                                               invoice_no=receipt.original_invoice_no).first()
        out["original_cu_no"] = original.cu_invoice_no if original else (
            f"{receipt.sdc_id}/{receipt.original_invoice_no}" if receipt.sdc_id
            else str(receipt.original_invoice_no))
    return out
