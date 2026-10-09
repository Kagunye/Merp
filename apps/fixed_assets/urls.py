from django.urls import path
from . import views

app_name = "fixed_assets"

urlpatterns = [
    path("", views.FixedAssetListView.as_view(), name="index"),
    path("assets/", views.FixedAssetListView.as_view(), name="asset_list"),
    path("assets/new/", views.FixedAssetCreateView.as_view(), name="asset_create"),
    path("assets/<uuid:pk>/", views.FixedAssetDetailView.as_view(), name="asset_detail"),
    path("assets/<uuid:pk>/depreciate/", views.DepreciationRunView.as_view(), name="asset_depreciate"),
    path("assets/<uuid:pk>/dispose/", views.AssetDisposalCreateView.as_view(), name="asset_dispose"),
    path("categories/", views.AssetCategoryListView.as_view(), name="category_list"),
    path("categories/new/", views.AssetCategoryCreateView.as_view(), name="category_create"),
]
