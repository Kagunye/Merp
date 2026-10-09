from django.urls import path
from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("register/", views.RegisterView.as_view(), name="register"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("profile/edit/", views.ProfileEditView.as_view(), name="profile_edit"),
    path("change-password/", views.ChangePasswordView.as_view(), name="change_password"),
    path("password-reset/", views.PasswordResetRequestView.as_view(), name="password_reset"),
    path("password-reset/<uuid:token>/", views.PasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("users/create/", views.UserCreateView.as_view(), name="user_create"),
    path("users/<uuid:pk>/", views.UserDetailView.as_view(), name="user_detail"),
    path("users/<uuid:pk>/edit/", views.UserEditView.as_view(), name="user_edit"),
    path("users/<uuid:pk>/toggle/", views.toggle_user_active, name="user_toggle"),
]
