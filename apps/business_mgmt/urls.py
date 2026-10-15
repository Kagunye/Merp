from django.urls import path
from . import views

app_name = "business_mgmt"

urlpatterns = [
    path("", views.BusinessMgmtIndexView.as_view(), name="index"),
]
