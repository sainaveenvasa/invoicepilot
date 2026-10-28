from django.urls import path

from . import views

# <str:…>, not <uuid:…>: a malformed ID reaches the selector and gets the
# same problem-JSON 404 as another workspace's ID, not Django's HTML 404.
urlpatterns = [
    path("customers", views.CustomerListView.as_view(), name="customer_list"),
    path("customers/<str:customer_id>", views.CustomerDetailView.as_view(), name="customer_detail"),
    path("customers/<str:customer_id>/deactivate", views.CustomerDeactivateView.as_view(), name="customer_deactivate"),
    path("customers/<str:customer_id>/reactivate", views.CustomerReactivateView.as_view(), name="customer_reactivate"),
]
