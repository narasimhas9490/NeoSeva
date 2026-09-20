from flask import Blueprint, g, request

from app.core.auth import require_user
from app.core.envelope import body, created, ok
from app.core.idempotency import idempotent
from app.customer.requests import service

bp = Blueprint("customer_requests", __name__, url_prefix="/customer/requests")


@bp.post("")
@require_user("CUSTOMER")
@idempotent
def create_request():
    """Post a request: the hardest write in the product.
    Every rule the app enforces is checked again, each with its own code.
    Returns 201 with the full request shape."""
    return created(service.create(g.user_id, body()))


@bp.get("")
@require_user("CUSTOMER")
def list_requests():
    """List her requests in the ACTIVE or PAST bucket.
    PAST is cursor-paged; nextCursor null means the end.
    ACTIVE is unpaged."""
    items, next_cursor = service.list_requests(g.user_id, request.args.get("bucket", "ACTIVE"), request.args.get("cursor"))
    return ok(items, nextCursor=next_cursor)


@bp.get("/<request_id>")
@require_user("CUSTOMER")
def get_request(request_id):
    """Read one of her requests.
    Carries a draft of what she entered while it can still be edited.
    Somebody else's request is 404."""
    return ok(service.read(g.user_id, request_id))


@bp.patch("/<request_id>")
@require_user("CUSTOMER")
@idempotent
def patch_request(request_id):
    """Edit a request nobody has offered on yet.
    Every create check runs again, because the cutoff may have passed.
    409 REQUEST_NOT_EDITABLE otherwise."""
    return ok(service.update(g.user_id, request_id, body()))


@bp.post("/<request_id>/cancel")
@require_user("CUSTOMER")
@idempotent
def cancel_request(request_id):
    """Call off a request before anybody is booked.
    A reason is optional, because the list may be empty.
    Cancelling twice is 200 both times."""
    return ok(service.cancel(g.user_id, request_id, body()))
