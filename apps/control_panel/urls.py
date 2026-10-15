from django.urls import path
from . import views

app_name = "control_panel"

urlpatterns = [
    path("", views.ControlPanelIndexView.as_view(), name="index"),
]
