"""Business Management launcher — a cross-module financial overview."""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView


class BusinessMgmtIndexView(LoginRequiredMixin, TemplateView):
    template_name = "business_mgmt/index.html"
