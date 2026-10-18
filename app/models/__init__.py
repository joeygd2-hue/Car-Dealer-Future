"""Import every model so SQLAlchemy metadata (and Alembic autogenerate) sees them."""
from app.models.core import *  # noqa: F401,F403
from app.models.costs import *  # noqa: F401,F403
from app.models.dealer_ops import *  # noqa: F401,F403
from app.models.finance import *  # noqa: F401,F403
from app.models.outreach import *  # noqa: F401,F403
from app.models.seo import *  # noqa: F401,F403
