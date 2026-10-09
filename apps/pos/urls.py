from django.urls import path
from . import views

app_name = "pos"

urlpatterns = [
    path("", views.ModuleHomeView.as_view(), name="home"),
    path("terminal/", views.TerminalView.as_view(), name="terminal"),
    path("sessions/", views.SessionListView.as_view(), name="session_list"),
    path("sessions/open/", views.SessionOpenView.as_view(), name="session_open"),
    path("sessions/<uuid:pk>/", views.SessionDetailView.as_view(), name="session_detail"),
    path("sessions/<uuid:pk>/close/", views.session_close, name="session_close"),
    path("sales/", views.SaleListView.as_view(), name="sale_list"),
    path("sales/<uuid:pk>/", views.SaleDetailView.as_view(), name="sale_detail"),
]
