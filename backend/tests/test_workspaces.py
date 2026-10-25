"""Workspaces, membership and owner permissions (FR-003, BR-010).

Most tests here set up two owners, A and B, and prove that A can never read
or change B's workspace through any route: selectors, the HTML app, the API
header, or a stale session.
"""

from unittest import mock

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.core import errors
from apps.core.context import RequestContext
from apps.organizations import permissions, selectors, services
from apps.organizations.middleware import SESSION_KEY
from apps.organizations.models import Membership, Organization

pytestmark = pytest.mark.django_db

ME = reverse("api:me")
WORKSPACE = reverse("api:workspace")


@pytest.fixture
def owner_a(make_owner):
    return make_owner("a@example.com", "Workspace A")


@pytest.fixture
def owner_b(make_owner):
    return make_owner("b@example.com", "Workspace B", writable=True)


def ctx_for(user, organization=None):
    membership = selectors.get_membership(user=user, organization_id=organization.pk) if organization else None
    return RequestContext.for_user(user, membership=membership)


# --- Creating a workspace -----------------------------------------------------


def test_create_workspace_makes_the_creator_its_only_owner(make_user):
    user = make_user()
    organization = services.create_workspace(
        ctx=RequestContext.for_user(user), name="  Reyes Studio ", timezone="Europe/Berlin", default_currency="EUR"
    )
    assert organization.name == "Reyes Studio"
    assert (organization.timezone, organization.default_currency) == ("Europe/Berlin", "EUR")
    membership = Membership.objects.get(organization=organization)
    assert (membership.user, membership.role) == (user, Membership.Role.OWNER)


def test_new_workspace_starts_read_only(owner_a):
    _, organization = owner_a
    assert organization.read_only_since is not None


@pytest.mark.parametrize(
    "fields",
    [{"name": "   "}, {"timezone": "Mars/Olympus_Mons"}, {"default_currency": "KWD"}, {"default_currency": "usd"}],
)
def test_create_workspace_validates_its_fields(make_user, fields):
    user = make_user()
    with pytest.raises(ValidationError):
        services.create_workspace(ctx=RequestContext.for_user(user), **{"name": "Studio", **fields})
    assert not Organization.objects.exists()


def test_create_workspace_is_all_or_nothing(make_user):
    user = make_user()
    with (
        mock.patch.object(Membership.objects, "create", side_effect=IntegrityError("boom")),
        pytest.raises(IntegrityError),
    ):
        services.create_workspace(ctx=RequestContext.for_user(user), name="Studio")
    assert not Organization.objects.exists()


def test_anonymous_context_cannot_create_a_workspace():
    with pytest.raises(errors.PermissionDenied):
        services.create_workspace(ctx=RequestContext.anonymous(), name="Studio")


def test_a_user_can_belong_to_a_workspace_only_once(owner_a):
    user, organization = owner_a
    with pytest.raises(IntegrityError, match="organizations_membership_uniq"), transaction.atomic():
        Membership.objects.create(organization=organization, user=user, role="owner")


def test_a_user_who_owns_a_workspace_cannot_be_deleted(owner_a):
    user, _ = owner_a
    with pytest.raises(IntegrityError), transaction.atomic():
        user.delete()


# --- Permissions --------------------------------------------------------------


def test_owner_may_perform_every_action(owner_a):
    user, organization = owner_a
    ctx = ctx_for(user, organization)
    assert all(permissions.has_permission(ctx, action) for action in permissions.ACTIONS)


def test_no_membership_means_no_permissions(make_user):
    ctx = RequestContext.for_user(make_user())
    assert not permissions.has_permission(ctx, "workspace.view")
    with pytest.raises(errors.NotFound):
        permissions.require(ctx, "workspace.view")


def test_unknown_action_is_a_programming_error(owner_a):
    with pytest.raises(ValueError, match="Unknown action"):
        permissions.has_permission(ctx_for(*owner_a), "workspace.updtae")


def test_suspended_workspace_refuses_every_action(owner_a):
    user, organization = owner_a
    Organization.objects.filter(pk=organization.pk).update(is_active=False)
    with pytest.raises(errors.WorkspaceSuspended):
        permissions.require(ctx_for(user, organization), "workspace.view")


def test_update_workspace_requires_write_access(owner_a):
    with pytest.raises(errors.ReadOnlyWorkspace):
        services.update_workspace(ctx=ctx_for(*owner_a), name="Renamed")


# --- Selectors ----------------------------------------------------------------


def test_memberships_for_user_lists_only_their_own(owner_a, owner_b):
    user_a, organization_a = owner_a
    assert [m.organization for m in selectors.memberships_for_user(user_a)] == [organization_a]


@pytest.mark.parametrize("bad_id", ["", "not-a-uuid", None, "00000000-0000-0000-0000-000000000000"])
def test_get_membership_rejects_malformed_and_unknown_ids(owner_a, bad_id):
    assert selectors.get_membership(user=owner_a[0], organization_id=bad_id) is None


def test_get_membership_of_another_users_workspace_is_none(owner_a, owner_b):
    assert selectors.get_membership(user=owner_a[0], organization_id=owner_b[1].pk) is None


def test_deactivated_user_has_no_memberships(owner_a):
    user, _ = owner_a
    user.is_active = False
    user.save()
    assert not selectors.memberships_for_user(user).exists()


# --- HTML app -------------------------------------------------------------------


def test_app_requires_login(client):
    response = client.get(reverse("app:dashboard"))
    assert response["Location"].startswith(reverse("accounts:login"))


def test_user_without_a_workspace_is_sent_to_create_one(make_user, client_for):
    client = client_for(make_user())
    response = client.get(reverse("app:dashboard"), follow=True)
    assert response.redirect_chain[-1][0] == reverse("app:workspace_create")


def test_creating_a_workspace_in_the_app_makes_it_active(make_user, client_for):
    user = make_user()
    client = client_for(user)
    form = {"name": "Reyes Studio", "timezone": "America/Denver", "default_currency": "USD"}
    response = client.post(reverse("app:workspace_create"), form)
    assert response["Location"] == reverse("app:dashboard")
    organization = Organization.objects.get()
    assert client.session[SESSION_KEY] == str(organization.pk)
    assert Membership.objects.get(organization=organization).user == user
    assert "Reyes Studio" in client.get(reverse("app:dashboard")).content.decode()


def test_dashboard_shows_only_the_users_workspace(owner_a, owner_b, client_for):
    content = client_for(owner_a[0]).get(reverse("app:dashboard")).content.decode()
    assert "Workspace A" in content
    assert "Workspace B" not in content


def test_selecting_another_users_workspace_is_404(owner_a, owner_b, client_for):
    client = client_for(owner_a[0])
    response = client.post(reverse("app:workspace_select", kwargs={"organization_id": owner_b[1].pk}))
    assert response.status_code == 404
    assert SESSION_KEY not in client.session


def test_selecting_a_workspace_requires_post(owner_a, client_for):
    url = reverse("app:workspace_select", kwargs={"organization_id": owner_a[1].pk})
    assert client_for(owner_a[0]).get(url).status_code == 405


def test_switching_between_own_workspaces(owner_a, make_owner, client_for):
    user, organization_a = owner_a
    second = services.create_workspace(ctx=RequestContext.for_user(user), name="Side Project")
    client = client_for(user)

    client.post(reverse("app:workspace_select", kwargs={"organization_id": second.pk}))
    assert "Side Project" in client.get(reverse("app:dashboard")).content.decode()
    client.post(reverse("app:workspace_select", kwargs={"organization_id": organization_a.pk}))
    assert "Workspace A" in client.get(reverse("app:dashboard")).content.decode()


def test_a_session_pointing_at_another_workspace_is_ignored(owner_a, owner_b, client_for):
    client = client_for(owner_a[0])
    session = client.session
    session[SESSION_KEY] = str(owner_b[1].pk)
    session.save()

    content = client.get(reverse("app:dashboard")).content.decode()

    assert "Workspace B" not in content
    assert "Workspace A" in content  # fell back to the user's only workspace
    assert client.session.get(SESSION_KEY) != str(owner_b[1].pk)


def test_removed_membership_takes_effect_on_the_next_request(owner_a, client_for):
    user, organization = owner_a
    client = client_for(user)
    client.post(reverse("app:workspace_select", kwargs={"organization_id": organization.pk}))
    assert client.get(reverse("app:dashboard")).status_code == 200

    Membership.objects.filter(user=user).delete()

    assert client.get(reverse("app:dashboard"))["Location"] == reverse("app:workspace_list")


def test_suspended_workspace_is_refused_in_the_app(owner_a, client_for):
    user, organization = owner_a
    Organization.objects.filter(pk=organization.pk).update(is_active=False)
    response = client_for(user).get(reverse("app:dashboard"))
    assert response.status_code == 403
    assert "suspended" in response.content.decode()


# --- API --------------------------------------------------------------------------


def assert_problem(response, status, code):
    assert response.status_code == status, response.content
    assert response["Content-Type"] == "application/problem+json"
    assert response.json()["code"] == code


def test_api_requires_authentication(client):
    assert_problem(client.get(ME), 401, "not_authenticated")
    assert_problem(client.get(WORKSPACE), 401, "not_authenticated")


def test_me_lists_only_the_users_own_workspaces(owner_a, owner_b, client_for):
    user, organization = owner_a
    body = client_for(user).get(ME).json()
    assert body["email"] == "a@example.com"
    assert body["email_verified"] is True
    assert body["memberships"] == [
        {"organization_id": str(organization.pk), "organization_name": "Workspace A", "role": "owner"}
    ]
    assert body["active_organization_id"] == str(organization.pk)


def test_get_workspace(owner_a, client_for):
    user, organization = owner_a
    body = client_for(user).get(WORKSPACE).json()
    assert body["id"] == str(organization.pk)
    assert body["name"] == "Workspace A"
    assert body["read_only"] is True
    assert body["created_at"].endswith("Z")


@pytest.mark.parametrize("path", [ME, WORKSPACE])
def test_workspace_header_must_match_membership(owner_a, owner_b, client_for, path):
    response = client_for(owner_a[0]).get(path, HTTP_X_WORKSPACE_ID=str(owner_b[1].pk))
    assert_problem(response, 404, "not_found")
    assert "Workspace B" not in response.content.decode()


def test_malformed_workspace_header_is_404(owner_a, client_for):
    assert_problem(client_for(owner_a[0]).get(WORKSPACE, HTTP_X_WORKSPACE_ID="../etc"), 404, "not_found")


def test_patch_on_another_workspace_is_404_and_changes_nothing(owner_a, owner_b, client_for):
    response = client_for(owner_a[0]).patch(
        WORKSPACE, {"name": "Pwned"}, content_type="application/json", HTTP_X_WORKSPACE_ID=str(owner_b[1].pk)
    )
    assert_problem(response, 404, "not_found")
    owner_b[1].refresh_from_db()
    assert owner_b[1].name == "Workspace B"


def test_patch_updates_a_writable_workspace(owner_b, client_for):
    user, organization = owner_b
    response = client_for(user).patch(
        WORKSPACE, {"name": " Renamed ", "timezone": "Asia/Tokyo"}, content_type="application/json"
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    organization.refresh_from_db()
    assert (organization.name, organization.timezone, organization.default_currency) == ("Renamed", "Asia/Tokyo", "USD")


def test_patch_on_a_read_only_workspace_is_refused(owner_a, client_for):
    user, organization = owner_a
    response = client_for(user).patch(WORKSPACE, {"name": "Renamed"}, content_type="application/json")
    assert_problem(response, 403, "workspace_read_only")
    organization.refresh_from_db()
    assert organization.name == "Workspace A"


@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        ({"nmae": "typo"}, "nmae", "Unknown field."),
        ({"read_only_since": None}, "read_only_since", "This field is read-only."),
        ({"id": "0192a8f0-5b1e-7c3d-9f4a-1b2c3d4e5f60"}, "id", "This field is read-only."),
        ({"name": "  "}, "name", "This field may not be blank."),
        ({"timezone": "Mars/Base"}, "timezone", "Mars/Base is not a valid IANA timezone."),
        ({"default_currency": "XYZ"}, "default_currency", "XYZ is not a supported currency."),
    ],
)
def test_patch_rejects_invalid_bodies(owner_b, client_for, body, field, message):
    user, organization = owner_b
    response = client_for(user).patch(WORKSPACE, body, content_type="application/json")
    assert_problem(response, 400, "validation_error")
    assert response.json()["errors"][field] == [message]
    organization.refresh_from_db()
    assert organization.name == "Workspace B"


def test_unsafe_api_methods_require_a_csrf_token(owner_b, client_for):
    user, organization = owner_b
    response = client_for(user, enforce_csrf_checks=True).patch(
        WORKSPACE, {"name": "Forged"}, content_type="application/json"
    )
    assert_problem(response, 403, "permission_denied")
    organization.refresh_from_db()
    assert organization.name == "Workspace B"


def test_user_with_several_workspaces_must_choose_one(owner_a, client_for):
    user, organization_a = owner_a
    second = services.create_workspace(ctx=RequestContext.for_user(user), name="Side Project")
    client = client_for(user)

    assert_problem(client.get(WORKSPACE), 404, "not_found")
    assert client.get(ME).json()["active_organization_id"] is None
    assert client.get(WORKSPACE, HTTP_X_WORKSPACE_ID=str(second.pk)).json()["name"] == "Side Project"


def test_suspended_workspace_is_refused_by_the_api(owner_a, client_for):
    user, organization = owner_a
    Organization.objects.filter(pk=organization.pk).update(is_active=False)
    client = client_for(user)
    assert_problem(client.get(WORKSPACE), 403, "workspace_suspended")
    assert client.get(ME).status_code == 200  # still able to see and pick other workspaces


def test_api_has_no_browsable_html(owner_a, client_for):
    response = client_for(owner_a[0]).get(WORKSPACE, HTTP_ACCEPT="text/html")
    assert response["Content-Type"] != "text/html; charset=utf-8"
