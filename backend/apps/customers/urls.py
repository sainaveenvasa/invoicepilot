from django.urls import path

from . import views

app_name = "customers"

urlpatterns = [
    path("", views.customer_list, name="list"),
    path("new/", views.customer_create, name="create"),
    path("<uuid:customer_id>/", views.customer_detail, name="detail"),
    path("<uuid:customer_id>/edit/", views.customer_edit, name="edit"),
    path("<uuid:customer_id>/deactivate/", views.customer_deactivate, name="deactivate"),
    path("<uuid:customer_id>/reactivate/", views.customer_reactivate, name="reactivate"),
    path("<uuid:customer_id>/delete/", views.customer_delete, name="delete"),
]
