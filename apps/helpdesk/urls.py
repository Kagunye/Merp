from django.urls import path
from . import views

app_name = "helpdesk"

urlpatterns = [
    path("", views.ModuleHomeView.as_view(), name="home"),
    path("tickets/", views.TicketListView.as_view(), name="ticket_list"),
    path("tickets/new/", views.TicketCreateView.as_view(), name="ticket_create"),
    path("tickets/<uuid:pk>/", views.TicketDetailView.as_view(), name="ticket_detail"),
    path("tickets/<uuid:pk>/comment/", views.ticket_comment, name="ticket_comment"),
    path("tickets/<uuid:pk>/resolve/", views.ticket_resolve, name="ticket_resolve"),
    path("tickets/<uuid:pk>/close/", views.ticket_close, name="ticket_close"),
]
