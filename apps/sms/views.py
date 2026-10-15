"""SMS messaging landing view."""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView


class SmsIndexView(LoginRequiredMixin, TemplateView):
    template_name = "sms/index.html"
