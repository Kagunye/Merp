from django.urls import path
from . import views

app_name = "documents"

urlpatterns = [
    path("", views.ModuleHomeView.as_view(), name="home"),
    path("files/", views.DocumentListView.as_view(), name="document_list"),
    path("files/upload/", views.DocumentUploadView.as_view(), name="document_upload"),
    path("files/<uuid:pk>/", views.DocumentDetailView.as_view(), name="document_detail"),
    path("files/<uuid:pk>/delete/", views.DocumentDeleteView.as_view(), name="document_delete"),
    path("categories/", views.CategoryListView.as_view(), name="category_list"),
    path("categories/new/", views.CategoryCreateView.as_view(), name="category_create"),
]
