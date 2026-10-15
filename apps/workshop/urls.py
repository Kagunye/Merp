from django.urls import path
from . import views

app_name = "workshop"

urlpatterns = [
    path("", views.WorkshopIndexView.as_view(), name="index"),
]
