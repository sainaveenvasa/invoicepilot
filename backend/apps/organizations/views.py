from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from . import selectors, services
from .forms import WorkspaceForm
from .middleware import set_active_workspace


def workspace_required(view):
    """Signed in, with an active workspace resolved by TenantMiddleware."""

    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        ctx = request.ctx
        if ctx.membership is None:
            return redirect("app:workspace_list")
        if not ctx.organization.is_active:
            return render(request, "organizations/suspended.html", status=403)
        return view(request, *args, **kwargs)

    return wrapper


@workspace_required
def dashboard(request):
    return render(request, "organizations/dashboard.html", {"workspace": request.ctx.organization})


@login_required
def workspace_list(request):
    memberships = selectors.memberships_for_user(request.user)
    if not memberships.exists():
        return redirect("app:workspace_create")
    return render(request, "organizations/workspace_list.html", {"memberships": memberships})


@login_required
@require_http_methods(["GET", "POST"])
def workspace_create(request):
    form = WorkspaceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            organization = services.create_workspace(ctx=request.ctx, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            set_active_workspace(request, organization)
            return redirect("app:dashboard")
    return render(request, "organizations/workspace_form.html", {"form": form})


@login_required
@require_POST
def workspace_select(request, organization_id):
    membership = selectors.get_membership(user=request.user, organization_id=organization_id)
    if membership is None:
        raise Http404  # not "forbidden": don't confirm the workspace exists
    set_active_workspace(request, membership.organization)
    return redirect("app:dashboard")
