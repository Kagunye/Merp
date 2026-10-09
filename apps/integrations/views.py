from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView


class ModuleHomeView(LoginRequiredMixin, TemplateView):
    template_name = "module_home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.__module__.split(".")[1].title()
        return ctx
