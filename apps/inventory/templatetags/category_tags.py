"""Template helpers for the product category tree."""
from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def category_nodes(context, company=None):
    """Every active category of the company in tree order, each a dict with
    ``id``, ``name``, ``label`` (the full path), ``level`` and ``parent``.

    The company is the one passed, or else the request's active company.
    """
    from apps.inventory.models import ProductCategory
    from apps.organizations.models import Company
    if not isinstance(company, Company):
        # Unset (or unresolved, which a template hands over as "").
        request = context.get("request")
        company = getattr(request, "active_company", None)
    if company is None:
        return []
    nodes, names = [], {}
    for node in ProductCategory.flat_tree(company):
        cat = node["category"]
        names[cat.pk] = cat.name
        parent_label = ""
        if cat.parent_id:
            parent_label = next((n["label"] for n in reversed(nodes) if n["id"] == str(cat.parent_id)), "")
        label = f"{parent_label} > {cat.name}" if parent_label else cat.name
        nodes.append({"id": str(cat.pk), "name": cat.name, "label": label,
                      "level": node["level"], "parent": str(cat.parent_id or "")})
    return nodes


@register.simple_tag
def category_path_ids(nodes, selected):
    """The ids from level 1 down to ``selected``, as a comma-separated string."""
    by_id = {n["id"]: n for n in nodes}
    chain, node = [], by_id.get(str(selected or ""))
    while node is not None and node["id"] not in chain:
        chain.insert(0, node["id"])
        node = by_id.get(node["parent"])
    return ",".join(chain)
