from django.urls import path
from . import views

app_name = "expenses"

urlpatterns = [
    path("", views.ModuleHomeView.as_view(), name="home"),
    path("claims/", views.ExpenseClaimListView.as_view(), name="claim_list"),
    path("claims/new/", views.ExpenseClaimCreateView.as_view(), name="claim_create"),
    path("claims/<uuid:pk>/", views.ExpenseClaimDetailView.as_view(), name="claim_detail"),
    path("claims/<uuid:pk>/edit/", views.ExpenseClaimUpdateView.as_view(), name="claim_edit"),
    path("claims/<uuid:pk>/line/", views.claim_add_line, name="claim_add_line"),
    path("claims/<uuid:pk>/submit/", views.claim_submit, name="claim_submit"),
    path("claims/<uuid:pk>/approve/", views.claim_approve, name="claim_approve"),
    path("claims/<uuid:pk>/reject/", views.claim_reject, name="claim_reject"),
    path("categories/", views.ExpenseCategoryListView.as_view(), name="category_list"),
    path("categories/new/", views.ExpenseCategoryCreateView.as_view(), name="category_create"),
]
