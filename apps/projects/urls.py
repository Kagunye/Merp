from django.urls import path
from . import views

app_name = "projects"

urlpatterns = [
    path("", views.ProjectListView.as_view(), name="index"),
    path("list/", views.ProjectListView.as_view(), name="project_list"),
    path("new/", views.ProjectCreateView.as_view(), name="project_create"),
    path("<uuid:pk>/", views.ProjectDetailView.as_view(), name="project_detail"),
    path("tasks/", views.TaskListView.as_view(), name="task_list"),
    path("tasks/<uuid:pk>/", views.TaskDetailView.as_view(), name="task_detail"),
    path("<uuid:project_pk>/tasks/new/", views.TaskCreateView.as_view(), name="task_create"),
]
