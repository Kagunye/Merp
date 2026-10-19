"""Template helpers for reporting pages."""
from django import template

register = template.Library()


@register.filter(is_safe=False)
def get_item(mapping, key):
    """Look up a dict key that template dot-notation cannot express (e.g. "0-30")."""
    try:
        return mapping[key]
    except (TypeError, LookupError):
        return ""
