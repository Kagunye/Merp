"""System configuration launcher."""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView


class ControlPanelIndexView(LoginRequiredMixin, TemplateView):
    template_name = "control_panel/index.html"
