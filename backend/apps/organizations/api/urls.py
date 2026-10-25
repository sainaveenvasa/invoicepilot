from django.urls import path

from . import views

app_name = "api"

urlpatterns = [
    path("me", views.MeView.as_view(), name="me"),
    path("workspace", views.WorkspaceView.as_view(), name="workspace"),
]
