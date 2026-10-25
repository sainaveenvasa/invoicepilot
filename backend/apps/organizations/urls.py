from django.urls import path

from . import views

app_name = "app"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("workspaces/", views.workspace_list, name="workspace_list"),
    path("workspaces/new/", views.workspace_create, name="workspace_create"),
    path("workspaces/<uuid:organization_id>/select/", views.workspace_select, name="workspace_select"),
]
