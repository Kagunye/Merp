from django.urls import path
from . import views

app_name = "pos"

urlpatterns = [
    path("", views.ModuleHomeView.as_view(), name="home"),
    path("terminal/", views.TillView.as_view(), name="terminal"),
    path("sessions/", views.SessionListView.as_view(), name="session_list"),
    path("sessions/open/", views.session_open, name="session_open"),
    path("sessions/<uuid:pk>/", views.SessionDetailView.as_view(), name="session_detail"),
    path("sessions/<uuid:pk>/close/", views.session_close, name="session_close"),
    path("sessions/<uuid:pk>/cash-up/", views.CashUpView.as_view(), name="cash_up"),
    path("sales/", views.SaleListView.as_view(), name="sale_list"),
    path("sales/<uuid:pk>/", views.SaleDetailView.as_view(), name="sale_detail"),
    path("sales/<uuid:pk>/refund/", views.sale_refund, name="sale_refund"),
]
