"""Small presentation helpers shared across templates."""
from django import template

register = template.Library()


@register.simple_tag
def page_url(request, page):
    """Link to `page` while keeping every other query parameter (filters, search)."""
    params = request.GET.copy()
    params["page"] = page
    return f"{request.path}?{params.urlencode()}"


@register.simple_tag
def page_window(paginator, current, span=2):
    """Page numbers to render around the current one — `span` neighbours each side."""
    start = max(1, current - span)
    end = min(paginator.num_pages, current + span)
    return list(range(start, end + 1))
