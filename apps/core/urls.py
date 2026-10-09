from django.shortcuts import redirect
from django.urls import path

urlpatterns = [
    path("", lambda request: redirect("dashboard:index"), name="home"),
]
