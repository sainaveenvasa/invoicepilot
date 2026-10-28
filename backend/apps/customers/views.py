from functools import wraps

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.cache import patch_vary_headers
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from apps.core import errors
from apps.core.htmx import is_htmx
from apps.organizations.views import workspace_required

from . import selectors, services
from .forms import CustomerEditForm, CustomerForm, CustomerSearchForm

PAGE_SIZE = 25

# Service errors shown on the form itself rather than as an error page.
FORM_ERRORS = (
    ValidationError,
    errors.Duplicate,
    errors.VersionConflict,
    errors.ReadOnlyWorkspace,
    errors.PlanLimitReached,
)


def domain_errors(view):
    """Map service errors that escape a view to pages (system design §4).
    Another workspace's ID is a 404, exactly like an unknown one."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except errors.NotFound as error:
            raise Http404 from error
        except errors.DomainError as error:
            return render(request, "customers/error.html", {"message": error.detail}, status=error.status)

    return wrapper


def show_error(form, error) -> int:
    """Attach a service error to the form; returns the response status."""
    if isinstance(error, ValidationError):
        form.add_error(None, error)
        return 422
    if isinstance(error, errors.Duplicate):
        form.add_error(error.field, error.detail)
        return 422
    form.add_error(None, error.detail)
    # The HTML convention for a stale version is 409, with a link to reload.
    return 409 if isinstance(error, errors.VersionConflict) else error.status


def page_context(request, **context):
    return {"workspace": request.ctx.organization, **context}


@workspace_required
@domain_errors
@require_safe
def customer_list(request):
    search = CustomerSearchForm(request.GET or None)
    q, status = search.search()
    customers = selectors.list_customers(ctx=request.ctx, q=q, is_active=CustomerSearchForm.IS_ACTIVE[status])
    page = Paginator(customers, PAGE_SIZE).get_page(request.GET.get("page"))
    context = page_context(request, page=page, q=q, status=status, status_choices=CustomerSearchForm.STATUS_CHOICES)
    template = "customers/_customer_results.html" if is_htmx(request) else "customers/customer_list.html"
    response = render(request, template, context)
    patch_vary_headers(response, ["HX-Request"])
    return response


@workspace_required
@domain_errors
@require_http_methods(["GET", "POST"])
def customer_create(request):
    form = CustomerForm(request.POST or None)
    status = 200
    if request.method == "POST":
        status = 422
        if form.is_valid():
            try:
                customer = services.create_customer(ctx=request.ctx, **form.cleaned_data)
            except FORM_ERRORS as error:
                status = show_error(form, error)
            else:
                messages.success(request, f"Added {customer.name}.")
                return redirect("customers:detail", customer_id=customer.pk)
    return render(request, "customers/customer_form.html", page_context(request, form=form), status=status)


@workspace_required
@domain_errors
@require_safe
def customer_detail(request, customer_id):
    customer = selectors.get_customer(ctx=request.ctx, customer_id=customer_id)
    return render(request, "customers/customer_detail.html", page_context(request, customer=customer))


@workspace_required
@domain_errors
@require_http_methods(["GET", "POST"])
def customer_edit(request, customer_id):
    customer = selectors.get_customer(ctx=request.ctx, customer_id=customer_id)
    status = 200
    if request.method == "POST":
        form = CustomerEditForm(request.POST)
        status = 422
        if form.is_valid():
            try:
                customer = services.update_customer(
                    ctx=request.ctx, customer_id=customer.pk, version=form.cleaned_data["version"], **form.changes()
                )
            except FORM_ERRORS as error:
                status = show_error(form, error)
            else:
                messages.success(request, f"Saved {customer.name}.")
                return redirect("customers:detail", customer_id=customer.pk)
    else:
        fields = ("name", "email", "company_name", "notes", "version")
        form = CustomerEditForm(initial={field: getattr(customer, field) for field in fields})
    context = page_context(request, form=form, customer=customer, conflict=status == 409)
    return render(request, "customers/customer_form.html", context, status=status)


def _change_state(request, customer_id, service, done: str):
    customer = service(ctx=request.ctx, customer_id=customer_id)
    if is_htmx(request):
        response = render(request, "customers/_customer_row.html", page_context(request, customer=customer))
        response["HX-Trigger"] = "customer-updated"
        return response
    messages.success(request, f"{customer.name} {done}.")
    return redirect("customers:detail", customer_id=customer.pk)


@workspace_required
@domain_errors
@require_POST
def customer_deactivate(request, customer_id):
    return _change_state(request, customer_id, services.deactivate_customer, "deactivated")


@workspace_required
@domain_errors
@require_POST
def customer_reactivate(request, customer_id):
    return _change_state(request, customer_id, services.reactivate_customer, "reactivated")


@workspace_required
@domain_errors
@require_http_methods(["GET", "POST"])
def customer_delete(request, customer_id):
    customer = selectors.get_customer(ctx=request.ctx, customer_id=customer_id)
    error = None
    if request.method == "POST":
        try:
            services.delete_customer(ctx=request.ctx, customer_id=customer.pk)
        except (errors.InUse, errors.ReadOnlyWorkspace) as exc:
            error = exc
        else:
            messages.success(request, f"Deleted {customer.name}.")
            return redirect("customers:list")
    context = page_context(request, customer=customer, error=error.detail if error else None)
    return render(request, "customers/customer_confirm_delete.html", context, status=error.status if error else 200)
