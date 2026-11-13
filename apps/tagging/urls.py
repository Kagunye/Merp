from django.urls import path

from . import views

app_name = "tagging"

urlpatterns = [
    path("", views.home, name="home"),
    path("lookup/", views.lookup, name="lookup"),
    path("batches/new/", views.batch_create, name="batch_create"),
    path("batches/<uuid:pk>/", views.batch_detail, name="batch_detail"),
    path("batches/<uuid:pk>/print/", views.batch_print, name="batch_print"),
    path("batches/<uuid:pk>/zpl/", views.batch_zpl, name="batch_zpl"),
    path("batches/<uuid:pk>/void/", views.batch_void, name="batch_void"),
    path("batches/<uuid:pk>/names/", views.batch_names, name="batch_names"),
    path("tags/<int:pk>/", views.tag_detail, name="tag_detail"),
    path("tags/<int:pk>/rename/", views.tag_rename, name="tag_rename"),
]
