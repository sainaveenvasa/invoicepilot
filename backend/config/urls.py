from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from apps.core import views as core_views
from apps.customers.api import urls as customers_api
from apps.organizations.api import urls as organizations_api

# Every module's /api/v1 routes share the "api" namespace.
api_v1 = ([*organizations_api.urlpatterns, *customers_api.urlpatterns], "api")

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="app:dashboard"), name="home"),
    path("", include("apps.accounts.urls")),
    path("app/customers/", include("apps.customers.urls")),
    path("app/", include("apps.organizations.urls")),
    path("api/v1/", include(api_v1)),
    path("healthz", core_views.healthz, name="healthz"),
    path("readyz", core_views.readyz, name="readyz"),
    # Moves to a separate host behind access control before launch (system design §12.4).
    path("admin/", admin.site.urls),
]
