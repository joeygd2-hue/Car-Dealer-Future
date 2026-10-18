"""The Alembic migrations produce exactly the schema the models describe."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from dealerflow.db import Base, get_engine


def test_migrations_match_models():
    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"render_as_batch": True}), Base.metadata)
    assert diff == [], diff
