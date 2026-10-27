"""Manufacturing views."""
import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import CreateView, DetailView, FormView, ListView, TemplateView

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
            form.fields["reference"].initial = SequenceCounter.next_number(company, "WO-")
        form.fields["planned_start"].initial = datetime.date.today()
        return form

    def form_valid(self, form):
        form.instance.company = self.request.active_company
        wo = form.save(commit=False)
        bom = wo.bom
        if not bom.production_quantity:
            form.add_error("bom", f"{bom.name} does not say how much one run produces, "
                                  "so its requirements cannot be scaled.")
            return self.form_invalid(form)
        ratio = wo.planned_quantity / bom.production_quantity
        lines = list(bom.lines.select_related("component__unit_of_measure", "unit_of_measure"))
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


def work_order_complete(request, pk):
    """POST — complete a work order, record stock movements."""
    from django.contrib.auth.decorators import login_required
    from apps.inventory.models import StockMovement

    wo = get_object_or_404(WorkOrder, pk=pk, company=request.active_company)
    if request.method != "POST":
        return redirect("manufacturing:work_order_detail", pk=pk)
    if wo.status == "DRAFT":
        messages.error(request, f"{wo.reference} is still a draft. Release it to the floor before completing it.")
        return redirect("manufacturing:work_order_detail", pk=pk)
    if wo.status not in ("RELEASED", "IN_PROGRESS"):
        messages.error(request, "Work order cannot be completed in its current state.")
        return redirect("manufacturing:work_order_detail", pk=pk)

    form = CompleteWorkOrderForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid form data.")
        return redirect("manufacturing:work_order_detail", pk=pk)

    actual_qty = form.cleaned_data["actual_quantity"]
    actual_end = form.cleaned_data["actual_end"]

    if not wo.warehouse:
        messages.error(request, "Work order must have a warehouse assigned.")
        return redirect("manufacturing:work_order_detail", pk=pk)

    if actual_qty is None or not actual_qty.is_finite() or actual_qty <= 0:
        messages.error(request, "Enter how many were actually made — a positive quantity.")
        return redirect("manufacturing:work_order_detail", pk=pk)

    from apps.inventory.models import StockLevel

    def shelf_cost(product):
        """What the shelf holds this product at, or its list cost when the
        shelf has never been costed."""
        level = StockLevel.objects.filter(product=product, warehouse=wo.warehouse).first()
        return level.avg_cost if level and level.avg_cost else product.cost_price

    try:
        with transaction.atomic():
            # Materials leave at what the book holds them at, into work in progress.
            used = Decimal("0")
            for consumption in wo.consumptions.select_related("product"):
                if consumption.planned_quantity > 0:
                    movement = StockMovement.objects.create(
                        product=consumption.product,
                        warehouse=wo.warehouse,
                        movement_type="PRODUCTION_OUT",
                        quantity=consumption.planned_quantity,
                        unit_cost=shelf_cost(consumption.product),
                        reference=wo.reference,
                        movement_date=actual_end,
                        performed_by=request.user,
                    )
                    used += movement.total_cost or Decimal("0")
                    consumption.actual_quantity = consumption.planned_quantity
                    consumption.save(update_fields=["actual_quantity"])

            # The output is worth what went into it, so work in progress empties.
            # With no materials (a recipe of labour only) it falls back to list cost.
            unit_cost = (used / actual_qty).quantize(Decimal("0.0001")) if used > 0 \
                else wo.bom.product.cost_price
            StockMovement.objects.create(
                product=wo.bom.product,
                warehouse=wo.warehouse,
                movement_type="PRODUCTION_IN",
                quantity=actual_qty,
                unit_cost=unit_cost,
                reference=wo.reference,
                movement_date=actual_end,
                performed_by=request.user,
            )

            wo.actual_quantity = actual_qty
            wo.actual_end = actual_end
            wo.status = "COMPLETED"
            wo.save(update_fields=["actual_quantity", "actual_end", "status", "updated_at"])
    except ValidationError as exc:
        messages.error(request, f"{wo.reference} was not completed: {' '.join(exc.messages)}")
        return redirect("manufacturing:work_order_detail", pk=pk)

    messages.success(request, f"Work Order {wo.reference} completed. Output: {actual_qty} {wo.bom.product.name}")
    return redirect("manufacturing:work_order_detail", pk=pk)


from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST


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
