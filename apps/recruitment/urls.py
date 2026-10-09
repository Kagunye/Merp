from django.urls import path
from . import views

app_name = "recruitment"

urlpatterns = [
    path("", views.ModuleHomeView.as_view(), name="home"),
    # Openings
    path("openings/", views.OpeningListView.as_view(), name="opening_list"),
    path("openings/new/", views.OpeningCreateView.as_view(), name="opening_create"),
    path("openings/<uuid:pk>/", views.OpeningDetailView.as_view(), name="opening_detail"),
    path("openings/<uuid:pk>/edit/", views.OpeningUpdateView.as_view(), name="opening_edit"),
    # Candidates
    path("candidates/", views.CandidateListView.as_view(), name="candidate_list"),
    path("candidates/new/", views.CandidateCreateView.as_view(), name="candidate_create"),
    path("candidates/<uuid:pk>/", views.CandidateDetailView.as_view(), name="candidate_detail"),
    # Interviews
    path("interviews/", views.InterviewListView.as_view(), name="interview_list"),
    path("interviews/new/", views.InterviewCreateView.as_view(), name="interview_create"),
]
