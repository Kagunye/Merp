"""Manufacturing views."""
import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, FormView, ListView, TemplateView

from apps.inventory.numbers import money, qty

from .forms import BOMForm, BOMLineFormSet, CompleteWorkOrderForm, WorkOrderForm
from .models import BillOfMaterials, WorkOrder, WorkOrderConsumption


class BOMListView(LoginRequiredMixin, ListView):
    template_name = "manufacturing/bom_list.html"
    context_object_name = "boms"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return BillOfMaterials.objects.none()
        qs = BillOfMaterials.objects.filter(company=company, is_active=True).select_related("product")
        q = self.request.GET.get("q")
        if q:
            from django.db.models import Q
            qs = qs.filter(Q(name__icontains=q) | Q(product__name__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Bills of Materials"
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class BOMCreateView(LoginRequiredMixin, CreateView):
    model = BillOfMaterials
    form_class = BOMForm
    template_name = "manufacturing/bom_form.html"

    def get_success_url(self):
        from django.urls import reverse
        return reverse("manufacturing:bom_detail", kwargs={"pk": self.object.pk})

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        company = self.request.active_company
        if company:
            from apps.inventory.models import Product
            form.fields["product"].queryset = Product.objects.filter(company=company, is_active=True)
        return form

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if self.request.POST:
            ctx["line_formset"] = BOMLineFormSet(self.request.POST, prefix="lines")
        else:
            ctx["line_formset"] = BOMLineFormSet(prefix="lines")
        if company:
            from apps.inventory.models import Product, UnitOfMeasure
            products = Product.objects.filter(company=company, is_active=True)
            uoms = UnitOfMeasure.objects.filter(company=company, is_active=True)
            for lform in ctx["line_formset"].forms:
                lform.fields["component"].queryset = products
                lform.fields["unit_of_measure"].queryset = uoms
        ctx["page_title"] = "New Bill of Materials"
        return ctx

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        ctx = self.get_context_data(form=form)
        formset = ctx["line_formset"]
        if formset.is_valid():
            self.object = form.save()
            formset.instance = self.object
            formset.save()
            messages.success(self.request, f"BOM '{self.object.name}' created.")
            return redirect(self.get_success_url())
        return self.render_to_response(ctx)


class BOMDetailView(LoginRequiredMixin, DetailView):
    model = BillOfMaterials
    template_name = "manufacturing/bom_detail.html"
    context_object_name = "bom"

    def get_queryset(self):
        return BillOfMaterials.objects.filter(company=self.request.active_company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"BOM: {self.object.name}"
        ctx["lines"] = self.object.lines.select_related("component", "unit_of_measure")
        ctx["work_orders"] = self.object.work_orders.order_by("-created_at")[:10]
        return ctx


class WorkOrderListView(LoginRequiredMixin, ListView):
    template_name = "manufacturing/work_order_list.html"
    context_object_name = "work_orders"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return WorkOrder.objects.none()
        qs = WorkOrder.objects.filter(company=company, is_active=True).select_related("bom__product")
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        q = self.request.GET.get("q")
        if q:
            from django.db.models import Q
            qs = qs.filter(Q(reference__icontains=q) | Q(bom__name__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Work Orders"
        ctx["status_choices"] = WorkOrder.STATUS_CHOICES
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class WorkOrderCreateView(LoginRequiredMixin, CreateView):
    model = WorkOrder
    form_class = WorkOrderForm
    template_name = "manufacturing/work_order_form.html"

    def get_success_url(self):
        from django.urls import reverse
        return reverse("manufacturing:work_order_detail", kwargs={"pk": self.object.pk})

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        company = self.request.active_company
        if company:
            form.fields["bom"].queryset = BillOfMaterials.objects.filter(company=company, status="ACTIVE", is_active=True)
            from apps.organizations.models import Warehouse
            form.fields["warehouse"].queryset = Warehouse.objects.filter(company=company, is_active=True)
            from apps.core.models import SequenceCounter
            # A preview: the number is taken at save time (claim_number), so
            # opening this form and leaving does not leave a gap in the series.
            form.fields["reference"].initial = SequenceCounter.peek_number(company, "WO-")
        form.fields["planned_start"].initial = datetime.date.today()
        return form

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        wo = form.save(commit=False)
        bom = wo.bom
        if not bom.production_quantity or bom.production_quantity <= 0:
            form.add_error("bom", f"{bom.name} does not say how much one run produces "
                                  "(it must be more than zero), so its requirements cannot "
                                  "be scaled. Correct the bill of materials first.")
            return self.form_invalid(form)
        ratio = wo.planned_quantity / bom.production_quantity
        lines = list(bom.lines.select_related("component__unit_of_measure", "unit_of_measure"))
        if not lines:
            form.add_error("bom", f"{bom.name} lists no materials, so an order made from it "
                                  "would put stock on the shelf from nothing. Add its "
                                  "components first.")
            return self.form_invalid(form)
        # Check the whole recipe converts before anything is written: a line
        # counted in tonnes against a component kept in pieces has no plan.
        planned_quantities = []
        for line in lines:
            try:
                planned_quantities.append(line.stock_quantity(ratio))
            except ValidationError as e:
                form.add_error(
                    None, f"{bom.name} — {line.component.name}: {e.messages[0]}")
                return self.form_invalid(form)
        with transaction.atomic():
            from apps.core.models import SequenceCounter
            company = wo.company
            wo.reference = SequenceCounter.claim_number(
                company, "WO-", wo.reference,
                taken=lambda ref: WorkOrder.objects.filter(company=company, reference=ref).exists())
            wo.save()
            self.object = wo
            for line, planned in zip(lines, planned_quantities):
                WorkOrderConsumption.objects.create(
                    work_order=wo,
                    product=line.component,
                    planned_quantity=planned,
                    actual_quantity=Decimal("0"),
                )
        messages.success(self.request, f"Work Order '{wo.reference}' created.")
        return redirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "New Work Order"
        return ctx


class WorkOrderDetailView(LoginRequiredMixin, DetailView):
    model = WorkOrder
    template_name = "manufacturing/work_order_detail.html"
    context_object_name = "wo"

    def get_queryset(self):
        return WorkOrder.objects.filter(company=self.request.active_company)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Work Order: {self.object.reference}"
        ctx["consumptions"] = self.object.consumptions.select_related("product")
        ctx["complete_form"] = CompleteWorkOrderForm(initial={"actual_end": datetime.date.today(), "actual_quantity": self.object.planned_quantity})
        return ctx


@login_required
@require_POST
def work_order_complete(request, pk):
    """Complete a work order: draw the materials, put the finished goods away.

    Materials leave the shelf at the average the shelf carries them for — the
    stock model prices every outgoing movement that way — and are charged to
    work in progress. The finished goods then come out of work in progress at
    exactly what went into it, so 1450 is empty again once the order is done
    and the finished item's shelf value is what it actually cost to make. The
    price typed on the product card is a guess about the future; using it for
    either side would leave the inventory account and the stock valuation
    disagreeing by the difference, with work in progress carrying the rest.

    A shelf that cannot cover the recipe stops the whole order: nothing is
    drawn, nothing is made, and the screen says which component is short.

    Untracked inputs — labour, a subcontracted service — have no shelf and so
    are never short. They are charged to work in progress at the cost price on
    their product card (Dr 1450, Cr 5250 Production Costs Absorbed), so the
    finished goods carry their labour as well as their materials. A finished
    product that is not stock-tracked has no shelf to go on either: what went
    into work in progress is expensed to its cost account on completion, so
    1450 is empty again either way.
    """
    from apps.accounting.gl_service import (
        post_production_absorption, post_production_rounding, post_untracked_output,
    )
    from apps.accounting.models import to_base
    from apps.inventory.models import StockMovement
    from apps.inventory.numbers import UNIT_COST, fits

    wo = get_object_or_404(WorkOrder, pk=pk, company=request.active_company)
    if wo.status not in ("RELEASED", "IN_PROGRESS", "DRAFT"):
        messages.error(
            request,
            f"Work order {wo.reference} is {wo.get_status_display().lower()}, so it cannot "
            "be completed. Only a draft, released or in-progress order can.")
        return redirect("manufacturing:work_order_detail", pk=pk)
    if not wo.consumptions.filter(planned_quantity__gt=0).exists():
        # Output with nothing drawn for it would be stock made from nothing:
        # goods on the shelf, at no cost, that nobody bought or made.
        messages.error(
            request,
            f"Work order {wo.reference} has no materials to draw — its recipe asks for "
            "nothing — so completing it would put stock on the shelf from nothing. "
            "Cancel it, and raise the order from a bill of materials that lists what "
            "the product is made of.")
        return redirect("manufacturing:work_order_detail", pk=pk)

    form = CompleteWorkOrderForm(request.POST)
    if not form.is_valid():
        problems = " ".join(" ".join(errs) for errs in form.errors.values())
        messages.error(request, f"Work order {wo.reference} was not completed. {problems}")
        return redirect("manufacturing:work_order_detail", pk=pk)

    actual_qty = form.cleaned_data["actual_quantity"]
    actual_end = form.cleaned_data["actual_end"]

    if not wo.warehouse:
        messages.error(request, "Work order must have a warehouse assigned.")
        return redirect("manufacturing:work_order_detail", pk=pk)
    if not wo.warehouse.is_active:
        messages.error(
            request,
            f"Work order {wo.reference} was not completed: its warehouse, "
            f"{wo.warehouse.name}, is inactive, so the finished goods cannot be put "
            "there. Reactivate the warehouse under Organization → Warehouses, or cancel "
            "this order and raise it again for an active warehouse.")
        return redirect("manufacturing:work_order_detail", pk=pk)
    # The completion date is the day the goods came off the line, and the
    # materials leave the shelf on it: it cannot be tomorrow, nor fall in a
    # month that has been closed. The ledger refuses a closed month for the
    # stock it books, but an order made only of untracked inputs and output
    # posts through other entries, so it is checked here for every order.
    from apps.inventory.models import stock_date_refusal
    refusal = stock_date_refusal(wo.company, actual_end, "A work order's completion")
    if refusal:
        messages.error(request, f"Work order {wo.reference} was not completed. {refusal}")
        return redirect("manufacturing:work_order_detail", pk=pk)

    product = wo.bom.product
    current = None
    try:
        with transaction.atomic():
            consumed = Decimal("0")
            for consumption in wo.consumptions.select_related("product"):
                if consumption.planned_quantity <= 0:
                    continue
                current = consumption.product
                if not consumption.product.track_inventory:
                    # Labour or a service: nothing to take off a shelf, so no
                    # stock check and no stock movement. Its cost goes into work
                    # in progress at the price on its card.
                    charged = to_base(consumption.planned_quantity
                                      * (consumption.product.cost_price or Decimal("0")),
                                      Decimal("1"))
                    if post_production_absorption(
                            wo.company, consumption.product, consumption.planned_quantity,
                            charged, actual_end, wo.reference, user=request.user):
                        consumed += charged
                else:
                    # No cost is given: the shelf prices what it gives up.
                    issue = StockMovement.objects.create(
                        product=consumption.product,
                        warehouse=wo.warehouse,
                        movement_type="PRODUCTION_OUT",
                        quantity=consumption.planned_quantity,
                        reference=wo.reference,
                        movement_date=actual_end,
                        performed_by=request.user,
                        notes=f"Issued to {wo.reference}",
                    )
                    # The figure work in progress was actually debited with:
                    # what the shelf's value fell by.
                    consumed += to_base(issue.total_cost, Decimal("1"))
                consumption.actual_quantity = consumption.planned_quantity
                consumption.save(update_fields=["actual_quantity"])

            current = product
            unit_cost = consumed / actual_qty if consumed > 0 else Decimal("0")
            if not fits(unit_cost, UNIT_COST):
                raise ValidationError(
                    f"{money(consumed)} of materials over {qty(actual_qty)} made is more per unit than "
                    "a stock record can hold — check the quantity made")
            unit_cost = unit_cost.quantize(Decimal("0.0001"))
            if product.track_inventory:
                output = StockMovement(
                    product=product,
                    warehouse=wo.warehouse,
                    movement_type="PRODUCTION_IN",
                    quantity=actual_qty,
                    unit_cost=unit_cost,
                    reference=wo.reference,
                    movement_date=actual_end,
                    performed_by=request.user,
                    notes=f"Made on {wo.reference}",
                )
                # The output comes onto the shelf at exactly what went into
                # work in progress, not at quantity × a four-place unit cost
                # that could miss it by a cent — so 1450 closes to nothing.
                output.carried_value = consumed
                output.save()
                made = to_base(output.total_cost, Decimal("1"))
                if consumed > 0 and made != consumed:
                    post_production_rounding(
                        wo.company, consumed - made, actual_end, wo.reference, user=request.user)
            else:
                # Nothing to put on a shelf: what was made is expensed now, and
                # work in progress is emptied of what went into it.
                post_untracked_output(
                    wo.company, product, actual_qty, consumed, actual_end, wo.reference,
                    user=request.user)

            wo.actual_quantity = actual_qty
            wo.actual_end = actual_end
            wo.cost = consumed
            wo.status = "COMPLETED"
            wo.save(update_fields=["actual_quantity", "actual_end", "cost", "status", "updated_at"])
    except ValidationError as exc:
        what = f" ({current.name})" if current else ""
        reason = exc.messages[0].rstrip()
        if not reason.endswith((".", "!", "?")):
            reason += "."
        # The advice has to fit the refusal: a shelf that ran short needs the
        # materials received (or a smaller run), but a closed period or a
        # posting the ledger refused says its own way forward, and telling
        # that bookkeeper to receive materials would send them the wrong way.
        short = "Insufficient stock" in reason
        advice = (" Receive the missing materials, or lower the quantity, and complete "
                  "it again." if short else "")
        messages.error(
            request,
            f"Work order {wo.reference} was not completed{what}: {reason} Nothing was "
            f"drawn from stock and nothing was posted.{advice}")
        return redirect("manufacturing:work_order_detail", pk=pk)

    messages.success(
        request,
        f"Work Order {wo.reference} completed. Output: {qty(actual_qty)} {product.name} "
        f"at {money(unit_cost)} each ({money(consumed)} of materials).")
    return redirect("manufacturing:work_order_detail", pk=pk)


@login_required
@require_POST
def work_order_release(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk, company=request.active_company)
    if wo.status != "DRAFT":
        messages.error(request, f"WO {wo.reference} cannot be released from {wo.get_status_display()}.")
    else:
        wo.status = "RELEASED"
        wo.save(update_fields=["status"])
        messages.success(request, f"WO {wo.reference} released to production.")
    return redirect("manufacturing:work_order_detail", pk=pk)


@login_required
@require_POST
def work_order_start(request, pk):
    from django.utils import timezone
    wo = get_object_or_404(WorkOrder, pk=pk, company=request.active_company)
    if wo.status != "RELEASED":
        messages.error(request, f"WO {wo.reference} must be RELEASED to start (currently {wo.get_status_display()}).")
    else:
        wo.status = "IN_PROGRESS"
        wo.actual_start = timezone.now().date()
        wo.save(update_fields=["status", "actual_start"])
        messages.success(request, f"WO {wo.reference} started.")
    return redirect("manufacturing:work_order_detail", pk=pk)


@login_required
@require_POST
def work_order_cancel(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk, company=request.active_company)
    if wo.status == "COMPLETED":
        messages.error(request, f"WO {wo.reference} is completed; cannot cancel.")
    else:
        wo.status = "CANCELLED"
        wo.save(update_fields=["status"])
        messages.success(request, f"WO {wo.reference} cancelled.")
    return redirect("manufacturing:work_order_detail", pk=pk)


class ManufacturingIndexView(LoginRequiredMixin, TemplateView):
    template_name = "manufacturing/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self.request.active_company
        if not company:
            return ctx
        wos = WorkOrder.objects.filter(company=company)
        ctx["active_count"] = wos.filter(status__in=["DRAFT", "RELEASED", "IN_PROGRESS"]).count()
        ctx["completed_count"] = wos.filter(status="COMPLETED").count()
        ctx["bom_count"] = BillOfMaterials.objects.filter(company=company).count()
        ctx["recent_orders"] = wos.select_related("bom").order_by("-created_at")[:8]
        return ctx
