from flask import Blueprint, g

from app.core.auth import require_user
from app.core.db import tx
from app.core.envelope import ok
from app.partner.identity.routes import identity_shape
from app.partner.jobs.routes import active_jobs
from app.partner.offers.service import sent_offers
from app.partner.opportunities.service import list_open
from app.partner.profile.service import profile_shape
from app.shared.settings import catalog_setting

bp = Blueprint("partner_home", __name__, url_prefix="/partner")


@bp.get("/home")
@require_user("PARTNER")
def home():
    """Return everything his home screen draws, in one call.
    Opportunities are the first jobsOnHomeScreen, a served number.
    creditBalance stays null until the chunk 5 ledger endpoints exist."""
    with tx() as conn:
        limit = catalog_setting(conn)["jobs_on_home_screen"]
        opportunities, _ = list_open(conn, g.user_id, limit=limit)
        return ok(
            {
                "profile": profile_shape(conn, g.user_id),
                "identityVerification": identity_shape(conn, g.user_id),
                "opportunities": opportunities,
                "activeJobs": active_jobs(conn, g.user_id),
                "sentOffers": sent_offers(conn, g.user_id, statuses=("ACTIVE",)),
                "creditBalance": None,
            }
        )
