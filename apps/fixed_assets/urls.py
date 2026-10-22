from django.urls import path
from . import views

app_name = "fixed_assets"

urlpatterns = [
    path("", views.FixedAssetsIndexView.as_view(), name="index"),
    path("assets/", views.FixedAssetListView.as_view(), name="asset_list"),
    path("assets/new/", views.FixedAssetCreateView.as_view(), name="asset_create"),
    path("assets/depreciation/run/", views.DepreciationRunAllView.as_view(),
         name="depreciation_run"),
    path("assets/<uuid:pk>/", views.FixedAssetDetailView.as_view(), name="asset_detail"),
    path("assets/<uuid:pk>/edit/", views.FixedAssetUpdateView.as_view(), name="asset_edit"),
    path("assets/<uuid:pk>/depreciate/", views.DepreciationRunView.as_view(),
         name="asset_depreciate"),
    path("assets/<uuid:pk>/dispose/", views.AssetDisposalCreateView.as_view(),
         name="asset_dispose"),
    path("assets/<uuid:pk>/acquisition/reverse/", views.acquisition_reverse,
         name="acquisition_reverse"),
    path("assets/<uuid:pk>/disposal/reverse/", views.disposal_reverse,
         name="disposal_reverse"),
    path("depreciations/<uuid:pk>/reverse/", views.depreciation_reverse,
         name="depreciation_reverse"),
    path("categories/", views.AssetCategoryListView.as_view(), name="category_list"),
    path("categories/new/", views.AssetCategoryCreateView.as_view(), name="category_create"),
]
