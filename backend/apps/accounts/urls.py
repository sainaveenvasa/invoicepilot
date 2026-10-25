from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("signup/", views.signup, name="signup"),
    path("signup/done/", views.signup_done, name="signup_done"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("verify-email/resend/", views.resend_verification, name="resend_verification"),
    path("verify-email/<str:token>/", views.verify_email, name="verify_email"),
    path("password-reset/", views.PasswordResetView.as_view(), name="password_reset"),
    path("password-reset/done/", views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path("reset/<uidb64>/<token>/", views.PasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("reset/done/", views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
]
