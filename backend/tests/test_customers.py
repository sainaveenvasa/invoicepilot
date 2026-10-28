"""Customer management (FR-004, API contracts §4) and its tenant isolation (threat model T1, T2).

Most tests set up two writable owners, A and B, and prove that A can never
read or change B's customers through any route: services, selectors, the
HTML app or the API.
"""

import hashlib
from pathlib import Path

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import Plan, Subscription
from apps.core import errors
from apps.core.context import RequestContext
from apps.customers import selectors, services
from apps.customers.models import Customer
from apps.organizations import selectors as organization_selectors
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db

CUSTOMERS = reverse("api:customer_list")


@pytest.fixture
def owner_a(make_owner):
    return make_owner("a@example.com", "Workspace A", writable=True)


@pytest.fixture
def owner_b(make_owner):
    return make_owner("b@example.com", "Workspace B", writable=True)


def ctx_for(user, organization):
    membership = organization_selectors.get_membership(user=user, organization_id=organization.pk)
    return RequestContext.for_user(user, membership=membership)


@pytest.fixture
def ctx_a(owner_a):
    return ctx_for(*owner_a)


@pytest.fixture
def ctx_b(owner_b):
    return ctx_for(*owner_b)


@pytest.fixture
def foreign(ctx_b):
    """A customer in workspace B that A must never see or touch."""
    return services.create_customer(ctx=ctx_b, name="Secret Client", email="secret@b.example")


def create(ctx, name="Acme Corp", email="ap@acme.example", **fields):
    return services.create_customer(ctx=ctx, name=name, email=email, **fields)


def subscribe(organization, max_customers):
    plan = Plan.objects.create(code="solo", name="Solo", stripe_price_id="price_solo", max_customers=max_customers)
    Subscription.objects.create(
        organization=organization,
        plan=plan,
        stripe_subscription_id="sub_1",
        status=Subscription.Status.ACTIVE,
        stripe_event_created=timezone.now(),
    )


def assert_problem(response, status, code):
    assert response.status_code == status, response.content
    assert response["Content-Type"] == "application/problem+json"
    assert response.json()["code"] == code


def assert_untouched(customer):
    fresh = Customer.objects.get(pk=customer.pk)
    assert (fresh.name, fresh.email, fresh.is_active, fresh.version) == (
        customer.name,
        customer.email,
        customer.is_active,
        customer.version,
    )


# --- Database constraints --------------------------------------------------------


def test_email_is_unique_per_workspace_but_not_across_workspaces(org, other_org, make_customer):
    make_customer(org, "ap@acme.example")
    make_customer(other_org, "ap@acme.example")
    with pytest.raises(IntegrityError, match="customers_customer_email_uniq"), transaction.atomic():
        make_customer(org, "AP@Acme.example")  # normalized on save, so it clashes


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"name": ""}, "customers_customer_name_set"),
        ({"email": ""}, "customers_customer_email_set"),
        ({"email": "AP@ACME.EXAMPLE"}, "customers_customer_email_lowercase"),
        ({"notes": "x" * 5001}, "customers_customer_notes_length"),
        ({"version": 0}, "customers_customer_version_positive"),
        ({"is_active": False}, "customers_customer_deactivation_consistent"),
        ({"deactivated_at": timezone.now()}, "customers_customer_deactivation_consistent"),
    ],
)
def test_database_rejects_invalid_customers_even_via_raw_update(org, make_customer, changes, constraint):
    customer = make_customer(org)
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        Customer.objects.filter(pk=customer.pk).update(**changes)


def test_new_customers_start_at_version_one(org, make_customer):
    assert make_customer(org).version == 1


# --- Services: create ------------------------------------------------------------------


def test_create_customer_normalizes_its_fields(ctx_a, owner_a):
    customer = create(ctx_a, name="  Acme Corp ", email=" AP@Acme.Example ", company_name=" Acme Ltd ")
    assert (customer.name, customer.email, customer.company_name) == ("Acme Corp", "ap@acme.example", "Acme Ltd")
    assert customer.organization == owner_a[1]
    assert (customer.is_active, customer.deactivated_at, customer.version) == (True, None, 1)


@pytest.mark.parametrize(
    "fields",
    [{"name": "   "}, {"email": "not-an-email"}, {"name": "x" * 201}, {"notes": "x" * 5001}, {"email": ""}],
)
def test_create_customer_validates_its_fields(ctx_a, fields):
    with pytest.raises(ValidationError):
        create(ctx_a, **fields)
    assert not Customer.objects.exists()


def test_duplicate_email_in_the_same_workspace_is_refused(ctx_a):
    existing = create(ctx_a, email="ap@acme.example")
    with pytest.raises(errors.Duplicate) as caught:
        create(ctx_a, name="Other", email="AP@ACME.example")
    assert caught.value.field == "email"
    assert caught.value.extra == {"existing_id": str(existing.pk)}
    assert Customer.objects.count() == 1


def test_same_email_in_another_workspace_is_allowed_and_not_revealed(ctx_a, foreign):
    customer = create(ctx_a, email=foreign.email)
    assert customer.pk != foreign.pk


def test_writes_need_a_writable_workspace(make_owner):
    user, organization = make_owner("ro@example.com", "Read-only")
    with pytest.raises(errors.ReadOnlyWorkspace):
        create(ctx_for(user, organization))


def test_writes_need_a_membership(make_user):
    with pytest.raises(errors.NotFound):
        create(RequestContext.for_user(make_user()))


def test_suspended_workspace_cannot_create_or_list(ctx_a, owner_a):
    Organization.objects.filter(pk=owner_a[1].pk).update(is_active=False)
    ctx = ctx_for(*owner_a)
    with pytest.raises(errors.WorkspaceSuspended):
        create(ctx)
    with pytest.raises(errors.WorkspaceSuspended):
        selectors.list_customers(ctx=ctx)


def test_plan_limit_counts_every_customer_including_deactivated(ctx_a, owner_a):
    subscribe(owner_a[1], max_customers=2)
    first = create(ctx_a, email="one@example.com")
    create(ctx_a, email="two@example.com")
    services.deactivate_customer(ctx=ctx_a, customer_id=first.pk)

    with pytest.raises(errors.PlanLimitReached) as caught:
        create(ctx_a, email="three@example.com")

    assert caught.value.extra == {"limit": 2, "current": 2}
    assert Customer.objects.count() == 2


def test_plan_limit_is_per_workspace(ctx_a, owner_a, ctx_b):
    subscribe(owner_a[1], max_customers=1)
    create(ctx_b, email="one@example.com")
    create(ctx_a, email="one@example.com")  # B's customer doesn't use A's allowance


def test_unlimited_plan_has_no_customer_limit(ctx_a, owner_a):
    subscribe(owner_a[1], max_customers=None)
    for n in range(3):
        create(ctx_a, email=f"c{n}@example.com")


# --- Services: update ----------------------------------------------------------------


def test_update_customer_changes_fields_and_bumps_version(ctx_a):
    customer = create(ctx_a)
    updated = services.update_customer(
        ctx=ctx_a, customer_id=customer.pk, version=1, name=" Acme Inc ", email="Billing@Acme.example", notes="Net 30"
    )
    assert (updated.name, updated.email, updated.notes, updated.version) == (
        "Acme Inc",
        "billing@acme.example",
        "Net 30",
        2,
    )
    assert Customer.objects.get(pk=customer.pk).version == 2


@pytest.mark.parametrize("stale", [1, 3, None], ids=["older", "newer", "missing"])
def test_stale_version_update_is_rejected(ctx_a, stale):
    customer = create(ctx_a)
    services.update_customer(ctx=ctx_a, customer_id=customer.pk, version=1, name="First save")

    with pytest.raises(errors.VersionConflict) as caught:
        services.update_customer(ctx=ctx_a, customer_id=customer.pk, version=stale, name="Lost")

    assert caught.value.current.name == "First save"
    assert caught.value.current.version == 2
    assert Customer.objects.get(pk=customer.pk).name == "First save"


def test_update_to_an_email_already_used_is_a_duplicate(ctx_a):
    taken = create(ctx_a, email="taken@example.com")
    customer = create(ctx_a, email="mine@example.com")
    with pytest.raises(errors.Duplicate) as caught:
        services.update_customer(ctx=ctx_a, customer_id=customer.pk, version=1, email="TAKEN@example.com")
    assert caught.value.extra["existing_id"] == str(taken.pk)
    assert Customer.objects.get(pk=customer.pk).email == "mine@example.com"


def test_keeping_the_same_email_is_not_a_duplicate(ctx_a):
    customer = create(ctx_a)
    services.update_customer(ctx=ctx_a, customer_id=customer.pk, version=1, email=customer.email.upper())


def test_update_refuses_system_owned_fields(ctx_a):
    customer = create(ctx_a)
    with pytest.raises(ValueError, match="Not updatable"):
        services.update_customer(ctx=ctx_a, customer_id=customer.pk, version=1, is_active=False)


# --- Services: deactivate, reactivate, delete ----------------------------------------


def test_deactivate_and_reactivate_are_idempotent(ctx_a):
    customer = create(ctx_a)

    deactivated = services.deactivate_customer(ctx=ctx_a, customer_id=customer.pk)
    assert (deactivated.is_active, deactivated.version) == (False, 2)
    assert deactivated.deactivated_at is not None
    again = services.deactivate_customer(ctx=ctx_a, customer_id=customer.pk)
    assert (again.version, again.deactivated_at) == (2, deactivated.deactivated_at)

    reactivated = services.reactivate_customer(ctx=ctx_a, customer_id=customer.pk)
    assert (reactivated.is_active, reactivated.deactivated_at, reactivated.version) == (True, None, 3)
    assert services.reactivate_customer(ctx=ctx_a, customer_id=customer.pk).version == 3


def test_customer_without_invoices_can_be_deleted(ctx_a):
    customer = create(ctx_a)
    services.delete_customer(ctx=ctx_a, customer_id=customer.pk)
    assert not Customer.objects.exists()


def test_customer_with_invoices_is_in_use(ctx_a, owner_a, make_invoice):
    customer = create(ctx_a)
    make_invoice(owner_a[1], customer=customer)
    with pytest.raises(errors.InUse):
        services.delete_customer(ctx=ctx_a, customer_id=customer.pk)
    assert Customer.objects.filter(pk=customer.pk).exists()


@pytest.mark.parametrize(
    "operation",
    [
        lambda ctx, pk: services.update_customer(ctx=ctx, customer_id=pk, version=1, name="Pwned"),
        lambda ctx, pk: services.deactivate_customer(ctx=ctx, customer_id=pk),
        lambda ctx, pk: services.reactivate_customer(ctx=ctx, customer_id=pk),
        lambda ctx, pk: services.delete_customer(ctx=ctx, customer_id=pk),
        lambda ctx, pk: selectors.get_customer(ctx=ctx, customer_id=pk),
    ],
    ids=["update", "deactivate", "reactivate", "delete", "get"],
)
def test_services_cannot_reach_another_workspaces_customer(ctx_a, foreign, operation):
    with pytest.raises(errors.NotFound):
        operation(ctx_a, foreign.pk)
    assert_untouched(foreign)


# --- Selectors -------------------------------------------------------------------------


@pytest.fixture
def directory(ctx_a):
    customers = {
        "acme": create(ctx_a, name="Acme Corp", email="ap@acme.example"),
        "beta": create(ctx_a, name="Beta LLC", email="finance@beta.example"),
        "zed": create(ctx_a, name="zed studio", email="hello@acme-partners.example"),
    }
    services.deactivate_customer(ctx=ctx_a, customer_id=customers["beta"].pk)
    return customers


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("", ["acme", "beta", "zed"]),
        ("ac", ["acme"]),  # name prefix
        ("ACME", ["acme"]),  # case-insensitive
        ("hello@", ["zed"]),  # email prefix
        ("corp", []),  # prefix, not substring
        ("%", []),  # LIKE wildcards are literal
        ("_", []),
        ("  beta ", ["beta"]),
    ],
)
def test_search_matches_the_start_of_name_or_email(ctx_a, directory, q, expected):
    found = list(selectors.list_customers(ctx=ctx_a, q=q))
    assert found == [directory[key] for key in expected]


def test_list_filters_by_active_state_and_orders(ctx_a, directory):
    assert list(selectors.list_customers(ctx=ctx_a, is_active=False)) == [directory["beta"]]
    assert list(selectors.list_customers(ctx=ctx_a, is_active=True)) == [directory["acme"], directory["zed"]]
    newest_first = list(selectors.list_customers(ctx=ctx_a, ordering="-created_at"))
    assert newest_first == [directory["zed"], directory["beta"], directory["acme"]]


def test_list_shows_only_the_workspaces_own_customers(ctx_a, directory, foreign):
    assert foreign not in selectors.list_customers(ctx=ctx_a)
    assert list(selectors.list_customers(ctx=ctx_a, q="secret")) == []


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "", None, "00000000-0000-0000-0000-000000000000"])
def test_get_customer_rejects_malformed_and_unknown_ids(ctx_a, bad_id):
    with pytest.raises(errors.NotFound):
        selectors.get_customer(ctx=ctx_a, customer_id=bad_id)


# --- API -------------------------------------------------------------------------------


def detail_url(customer, action=None):
    name = f"api:customer_{action}" if action else "api:customer_detail"
    return reverse(name, kwargs={"customer_id": customer.pk})


@pytest.fixture
def api_a(owner_a, client_for):
    return client_for(owner_a[0])


def post_json(client, url, body=None, **headers):
    return client.post(url, body or {}, content_type="application/json", **headers)


def patch_json(client, url, body, **headers):
    return client.patch(url, body, content_type="application/json", **headers)


def test_api_requires_authentication(client):
    assert_problem(client.get(CUSTOMERS), 401, "not_authenticated")


def test_api_create_returns_201_with_location_and_etag(api_a):
    response = post_json(api_a, CUSTOMERS, {"name": "Acme Corp", "email": "AP@acme.example"})
    assert response.status_code == 201, response.content
    body = response.json()
    customer = Customer.objects.get()
    assert response["Location"] == detail_url(customer)
    assert response["ETag"] == '"1"'
    assert body == {
        "id": str(customer.pk),
        "name": "Acme Corp",
        "email": "ap@acme.example",
        "company_name": "",
        "notes": "",
        "is_active": True,
        "deactivated_at": None,
        "version": 1,
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
    }
    assert body["created_at"].endswith("Z")


@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        ({"name": "Acme", "email": "ap@acme.example", "nmae": "x"}, "nmae", "Unknown field."),
        ({"name": "Acme", "email": "ap@acme.example", "version": 7}, "version", "This field is read-only."),
        ({"name": "Acme", "email": "ap@acme.example", "is_active": False}, "is_active", "This field is read-only."),
        ({"name": "Acme", "email": "ap@acme.example", "organization": "x"}, "organization", "Unknown field."),
        ({"name": "  ", "email": "ap@acme.example"}, "name", "This field may not be blank."),
        ({"name": "Acme", "email": "nope"}, "email", "Enter a valid email address."),
        ({"email": "ap@acme.example"}, "name", "This field is required."),
    ],
)
def test_api_create_rejects_invalid_bodies(api_a, body, field, message):
    response = post_json(api_a, CUSTOMERS, body)
    assert_problem(response, 400, "validation_error")
    assert response.json()["errors"][field] == [message]
    assert not Customer.objects.exists()


def test_api_create_duplicate_email_is_409_with_existing_id(api_a, ctx_a):
    existing = create(ctx_a)
    response = post_json(api_a, CUSTOMERS, {"name": "Again", "email": existing.email.upper()})
    assert_problem(response, 409, "duplicate")
    assert response.json()["existing_id"] == str(existing.pk)


def test_api_create_over_the_plan_limit_is_403(api_a, ctx_a, owner_a):
    subscribe(owner_a[1], max_customers=1)
    create(ctx_a)
    response = post_json(api_a, CUSTOMERS, {"name": "Second", "email": "two@example.com"})
    assert_problem(response, 403, "plan_limit_reached")
    assert (response.json()["limit"], response.json()["current"]) == (1, 1)


def test_api_writes_on_a_read_only_workspace_are_refused(make_owner, client_for):
    user, organization = make_owner("ro@example.com", "Read-only")
    customer = Customer.objects.create(organization=organization, name="Kept", email="kept@example.com")
    client = client_for(user)

    assert_problem(post_json(client, CUSTOMERS, {"name": "N", "email": "n@example.com"}), 403, "workspace_read_only")
    assert_problem(patch_json(client, detail_url(customer), {"name": "X"}), 403, "workspace_read_only")
    assert_problem(post_json(client, detail_url(customer, "deactivate")), 403, "workspace_read_only")
    assert_problem(client.delete(detail_url(customer)), 403, "workspace_read_only")
    # Reads still work (decision D3).
    assert client.get(CUSTOMERS).json()["results"][0]["name"] == "Kept"
    assert_untouched(customer)


def test_api_list_uses_cursor_pagination(api_a, ctx_a):
    for name in ["Cara", "Abe", "Bea"]:
        create(ctx_a, name=name, email=f"{name.lower()}@example.com")

    first = api_a.get(CUSTOMERS, {"page_size": 2}).json()
    assert set(first) == {"results", "next", "previous"}  # no total count
    assert [c["name"] for c in first["results"]] == ["Abe", "Bea"]
    second = api_a.get(first["next"]).json()
    assert [c["name"] for c in second["results"]] == ["Cara"]
    assert second["next"] is None


def test_api_list_page_size_is_capped(api_a, ctx_a):
    for n in range(3):
        create(ctx_a, name=f"C{n}", email=f"c{n}@example.com")
    assert len(api_a.get(CUSTOMERS, {"page_size": 1000}).json()["results"]) == 3  # max 100, no error


def test_api_list_filters_and_orders(api_a, ctx_a, directory):
    def names(**params):
        return [c["name"] for c in api_a.get(CUSTOMERS, params).json()["results"]]

    assert names() == ["Acme Corp", "Beta LLC", "zed studio"]
    assert names(q="AC") == ["Acme Corp"]
    assert names(is_active="false") == ["Beta LLC"]
    assert names(is_active="true", ordering="-created_at") == ["zed studio", "Acme Corp"]


@pytest.mark.parametrize("params", [{"ordering": "email"}, {"is_active": "maybe"}, {"q": "x" * 201}])
def test_api_list_rejects_invalid_filters(api_a, params):
    assert_problem(api_a.get(CUSTOMERS, params), 400, "validation_error")


def test_api_get_returns_the_customer_with_etag(api_a, ctx_a):
    customer = create(ctx_a)
    response = api_a.get(detail_url(customer))
    assert response.status_code == 200
    assert response.json()["email"] == "ap@acme.example"
    assert response["ETag"] == '"1"'


def test_patch_requires_if_match(api_a, ctx_a):
    customer = create(ctx_a)
    assert_problem(patch_json(api_a, detail_url(customer), {"name": "New"}), 428, "precondition_required")
    assert_untouched(customer)


@pytest.mark.parametrize("if_match", ['"1"', "1", "*", 'W/"2"', "garbage"])
def test_patch_with_a_stale_or_malformed_version_is_412_with_current(api_a, ctx_a, if_match):
    customer = create(ctx_a)
    services.update_customer(ctx=ctx_a, customer_id=customer.pk, version=1, name="Saved first")

    response = patch_json(api_a, detail_url(customer), {"name": "Lost"}, HTTP_IF_MATCH=if_match)

    assert_problem(response, 412, "version_conflict")
    assert response.json()["current"]["name"] == "Saved first"
    assert response.json()["current"]["version"] == 2
    assert response["ETag"] == '"2"'
    assert Customer.objects.get(pk=customer.pk).name == "Saved first"


def test_patch_with_the_current_version_saves(api_a, ctx_a):
    customer = create(ctx_a)
    response = patch_json(api_a, detail_url(customer), {"company_name": "Acme Ltd"}, HTTP_IF_MATCH='"1"')
    assert response.status_code == 200, response.content
    assert (response.json()["company_name"], response.json()["version"]) == ("Acme Ltd", 2)
    assert response["ETag"] == '"2"'


@pytest.mark.parametrize(
    ("body", "field"),
    [({"is_active": False}, "is_active"), ({"version": 9}, "version"), ({"notes": "x" * 5001}, "notes")],
)
def test_patch_rejects_invalid_bodies(api_a, ctx_a, body, field):
    customer = create(ctx_a)
    response = patch_json(api_a, detail_url(customer), body, HTTP_IF_MATCH='"1"')
    assert_problem(response, 400, "validation_error")
    assert field in response.json()["errors"]
    assert_untouched(customer)


def test_patch_to_a_taken_email_is_409(api_a, ctx_a):
    create(ctx_a, email="taken@example.com")
    customer = create(ctx_a, email="mine@example.com")
    response = patch_json(api_a, detail_url(customer), {"email": "taken@example.com"}, HTTP_IF_MATCH='"1"')
    assert_problem(response, 409, "duplicate")


def test_api_deactivate_and_reactivate(api_a, ctx_a):
    customer = create(ctx_a)
    response = post_json(api_a, detail_url(customer, "deactivate"))
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert response.json()["deactivated_at"].endswith("Z")
    assert post_json(api_a, detail_url(customer, "deactivate")).json()["version"] == 2  # idempotent
    response = post_json(api_a, detail_url(customer, "reactivate"))
    assert (response.json()["is_active"], response.json()["deactivated_at"]) == (True, None)


def test_api_delete(api_a, ctx_a, owner_a, make_invoice):
    unused = create(ctx_a, email="unused@example.com")
    invoiced = create(ctx_a, email="invoiced@example.com")
    make_invoice(owner_a[1], customer=invoiced)

    assert api_a.delete(detail_url(unused)).status_code == 204
    assert_problem(api_a.delete(detail_url(invoiced)), 409, "in_use")
    assert list(Customer.objects.all()) == [invoiced]


def test_unsafe_customer_api_methods_require_a_csrf_token(owner_a, client_for):
    client = client_for(owner_a[0], enforce_csrf_checks=True)
    assert_problem(post_json(client, CUSTOMERS, {"name": "N", "email": "n@example.com"}), 403, "permission_denied")
    assert not Customer.objects.exists()


@pytest.mark.parametrize("customer_id", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_api_malformed_and_unknown_ids_are_problem_404s(api_a, customer_id):
    url = reverse("api:customer_detail", kwargs={"customer_id": customer_id})
    assert_problem(api_a.get(url), 404, "not_found")


API_ROUTES = [
    ("get", None, {}),
    ("patch", None, {"HTTP_IF_MATCH": '"1"'}),
    ("delete", None, {}),
    ("post", "deactivate", {}),
    ("post", "reactivate", {}),
]


@pytest.mark.parametrize(("method", "action", "headers"), API_ROUTES)
def test_cross_workspace_ids_return_404(api_a, foreign, method, action, headers):
    """Threat model T2: every {id} route and method answers 404 for another
    workspace's customer, reveals nothing about it, and changes nothing."""
    url = detail_url(foreign, action)
    response = getattr(api_a, method)(url, {"name": "Pwned"}, content_type="application/json", **headers)
    assert_problem(response, 404, "not_found")
    assert "Secret" not in response.content.decode()
    assert_untouched(foreign)


@pytest.mark.parametrize(("method", "action", "headers"), [*API_ROUTES, ("get", "list", {})])
def test_workspace_header_for_another_workspace_is_404(api_a, foreign, method, action, headers):
    url = CUSTOMERS if action == "list" else detail_url(foreign, action)
    response = getattr(api_a, method)(
        url, content_type="application/json", HTTP_X_WORKSPACE_ID=str(foreign.organization_id), **headers
    )
    assert_problem(response, 404, "not_found")
    assert_untouched(foreign)


def test_api_list_never_includes_another_workspaces_customers(api_a, ctx_a, foreign):
    create(ctx_a, email=foreign.email)
    results = api_a.get(CUSTOMERS, {"q": "secret"}).json()["results"] + api_a.get(CUSTOMERS).json()["results"]
    assert str(foreign.pk) not in {c["id"] for c in results}


# --- HTML app and HTMX ---------------------------------------------------------------


LIST = reverse("customers:list")


def html_url(name, customer):
    return reverse(f"customers:{name}", kwargs={"customer_id": customer.pk})


def test_customer_pages_require_login(client):
    assert client.get(LIST)["Location"].startswith(reverse("accounts:login"))


def test_list_page_shows_only_own_customers(api_a, ctx_a, foreign):
    create(ctx_a)
    response = api_a.get(LIST)
    content = response.content.decode()
    assert response.status_code == 200
    assert "Acme Corp" in content
    assert "Secret Client" not in content
    assert "<html" in content
    assert "HX-Request" in response["Vary"]


def test_htmx_search_returns_only_the_results_fragment(api_a, directory):
    response = api_a.get(LIST, {"q": "acme", "status": "all"}, HTTP_HX_REQUEST="true")
    content = response.content.decode()
    assert response.status_code == 200
    assert "<html" not in content
    assert "Acme Corp" in content
    assert "Beta LLC" not in content
    assert "HX-Request" in response["Vary"]


def test_history_restore_gets_the_full_page(api_a):
    response = api_a.get(LIST, HTTP_HX_REQUEST="true", HTTP_HX_HISTORY_RESTORE_REQUEST="true")
    assert "<html" in response.content.decode()


@pytest.mark.parametrize(("status", "shown"), [("active", "Acme Corp"), ("inactive", "Beta LLC"), ("bogus", "Acme")])
def test_list_status_filter(api_a, directory, status, shown):
    content = api_a.get(LIST, {"status": status}).content.decode()
    assert shown in content
    if status == "inactive":
        assert "Acme Corp" not in content


def test_list_paginates(api_a, ctx_a):
    for n in range(26):
        create(ctx_a, name=f"Client {n:02d}", email=f"c{n}@example.com")
    first = api_a.get(LIST).content.decode()
    assert "Client 24" in first
    assert "Client 25" not in first
    assert "Page 1 of 2" in first
    second = api_a.get(LIST, {"page": 2}, HTTP_HX_REQUEST="true").content.decode()
    assert "Client 25" in second
    assert "Client 00" not in second


def test_customer_names_are_escaped(api_a, ctx_a):
    create(ctx_a, name="<script>alert(1)</script>")
    content = api_a.get(LIST).content.decode()
    assert "<script>alert(1)</script>" not in content
    assert "&lt;script&gt;" in content


def test_create_in_the_app_redirects_to_the_customer(api_a):
    response = api_a.post(reverse("customers:create"), {"name": "Acme Corp", "email": "AP@acme.example"})
    customer = Customer.objects.get()
    assert response["Location"] == html_url("detail", customer)
    assert customer.email == "ap@acme.example"


@pytest.mark.parametrize(
    ("form", "message"),
    [({"name": "", "email": "a@example.com"}, "This field is required."), ({"name": "A", "email": "x"}, "valid email")],
)
def test_invalid_create_rerenders_with_422(api_a, form, message):
    response = api_a.post(reverse("customers:create"), form)
    assert response.status_code == 422
    assert message in response.content.decode()
    assert not Customer.objects.exists()


def test_duplicate_email_in_the_app_is_a_field_error(api_a, ctx_a):
    create(ctx_a)
    response = api_a.post(reverse("customers:create"), {"name": "Again", "email": "ap@acme.example"})
    assert response.status_code == 422
    assert "A customer with this email already exists." in response.content.decode()


def test_create_in_a_read_only_workspace_is_refused(make_owner, client_for):
    user, _ = make_owner("ro@example.com", "Read-only")
    response = client_for(user).post(reverse("customers:create"), {"name": "Acme", "email": "a@example.com"})
    assert response.status_code == 403
    assert "read-only" in response.content.decode()
    assert not Customer.objects.exists()


def test_edit_in_the_app_saves_and_bumps_version(api_a, ctx_a):
    customer = create(ctx_a)
    form = api_a.get(html_url("edit", customer)).content.decode()
    assert 'name="version" value="1"' in form
    response = api_a.post(html_url("edit", customer), {"name": "Acme Inc", "email": customer.email, "version": 1})
    assert response["Location"] == html_url("detail", customer)
    customer.refresh_from_db()
    assert (customer.name, customer.version) == ("Acme Inc", 2)


def test_edit_with_a_stale_version_shows_a_conflict(api_a, ctx_a):
    customer = create(ctx_a)
    services.update_customer(ctx=ctx_a, customer_id=customer.pk, version=1, name="Saved elsewhere")
    response = api_a.post(html_url("edit", customer), {"name": "Mine", "email": customer.email, "version": 1})
    assert response.status_code == 409
    assert "changed by someone else" in response.content.decode()
    assert Customer.objects.get(pk=customer.pk).name == "Saved elsewhere"


def test_htmx_deactivate_returns_the_updated_row(api_a, ctx_a):
    customer = create(ctx_a)
    response = api_a.post(html_url("deactivate", customer), HTTP_HX_REQUEST="true")
    content = response.content.decode()
    assert response.status_code == 200
    assert response["HX-Trigger"] == "customer-updated"
    assert content.lstrip().startswith(f'<tr id="customer-{customer.pk}">')
    assert "Reactivate" in content
    assert Customer.objects.get(pk=customer.pk).is_active is False


def test_plain_deactivate_redirects_to_the_customer(api_a, ctx_a):
    customer = create(ctx_a)
    response = api_a.post(html_url("deactivate", customer))
    assert response["Location"] == html_url("detail", customer)
    api_a.post(html_url("reactivate", customer))
    assert Customer.objects.get(pk=customer.pk).is_active is True


def test_state_changes_require_post(api_a, ctx_a):
    customer = create(ctx_a)
    assert api_a.get(html_url("deactivate", customer)).status_code == 405
    assert Customer.objects.get(pk=customer.pk).is_active is True


def test_delete_in_the_app(api_a, ctx_a, owner_a, make_invoice):
    unused = create(ctx_a, email="unused@example.com")
    invoiced = create(ctx_a, email="invoiced@example.com")
    make_invoice(owner_a[1], customer=invoiced)

    assert api_a.post(html_url("delete", unused))["Location"] == LIST
    response = api_a.post(html_url("delete", invoiced))
    assert response.status_code == 409
    assert "Deactivate it instead" in response.content.decode()
    assert list(Customer.objects.all()) == [invoiced]


@pytest.mark.parametrize(
    ("method", "name", "data"),
    [
        ("get", "detail", {}),
        ("get", "edit", {}),
        ("post", "edit", {"name": "Pwned", "email": "secret@b.example", "version": 1}),
        ("post", "deactivate", {}),
        ("post", "reactivate", {}),
        ("get", "delete", {}),
        ("post", "delete", {}),
    ],
)
def test_html_routes_return_404_for_another_workspaces_customer(api_a, foreign, method, name, data):
    response = getattr(api_a, method)(html_url(name, foreign), data)
    assert response.status_code == 404
    assert "Secret Client" not in response.content.decode()
    assert_untouched(foreign)


# --- Vendored HTMX (threat model T3) -------------------------------------------------


HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"  # htmx.org@2.0.11 dist/htmx.min.js


def test_vendored_htmx_checksum():
    path = Path(settings.BASE_DIR) / "static" / "vendor" / "htmx-2.0.11.min.js"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == HTMX_SHA256


def test_pages_load_htmx_from_our_own_origin(api_a):
    content = api_a.get(LIST).content.decode()
    assert '<script src="/static/vendor/htmx-2.0.11.min.js" defer></script>' in content
    assert '"allowEval": false' in content
    assert "hx-headers" in content
