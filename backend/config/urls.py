from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from apps.core import views as core_views

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="app:dashboard"), name="home"),
    path("", include("apps.accounts.urls")),
    path("app/", include("apps.organizations.urls")),
    path("api/v1/", include("apps.organizations.api.urls")),
    path("healthz", core_views.healthz, name="healthz"),
    path("readyz", core_views.readyz, name="readyz"),
    # Moves to a separate host behind access control before launch (system design §12.4).
    path("admin/", admin.site.urls),
]
