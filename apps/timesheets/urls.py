from django.urls import path
from . import views

app_name = "timesheets"

urlpatterns = [
    path("", views.ModuleHomeView.as_view(), name="home"),
    path("entries/", views.EntryListView.as_view(), name="entry_list"),
    path("entries/new/", views.EntryCreateView.as_view(), name="entry_create"),
    path("entries/<uuid:pk>/", views.EntryDetailView.as_view(), name="entry_detail"),
    path("entries/<uuid:pk>/submit/", views.entry_submit, name="entry_submit"),
    path("entries/<uuid:pk>/approve/", views.entry_approve, name="entry_approve"),
]
