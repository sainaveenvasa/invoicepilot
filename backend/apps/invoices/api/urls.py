from django.urls import path

from . import views

# <str:…>, not <uuid:…>: a malformed ID reaches the selector and gets the
# same problem-JSON 404 as another workspace's ID, not Django's HTML 404.
urlpatterns = [
    path("invoices", views.InvoiceListView.as_view(), name="invoice_list"),
    path("invoices/<str:invoice_id>", views.InvoiceDetailView.as_view(), name="invoice_detail"),
]
