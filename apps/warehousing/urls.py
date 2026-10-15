from django.urls import path
from . import views

app_name = "warehousing"

urlpatterns = [
    path("", views.WarehousingIndexView.as_view(), name="index"),
]
