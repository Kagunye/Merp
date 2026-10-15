from django.urls import path
from . import views

app_name = "sms"

urlpatterns = [
    path("", views.SmsIndexView.as_view(), name="index"),
]
