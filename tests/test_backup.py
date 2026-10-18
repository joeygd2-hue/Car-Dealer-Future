import pytest

from dealerflow.backup import backup, restore
from dealerflow.config import get_settings
from dealerflow.db import reset_engine, session_scope
from dealerflow.models import Prospect
from dealerflow.services.discovery import run_source
from tests.conftest import PG_URL


@pytest.mark.skipif(bool(PG_URL), reason="SQLite backup path")
def test_sqlite_backup_and_restore(tmp_path):
    with session_scope() as db:
        run_source(db, "fixture")
    res = backup(str(tmp_path / "bk"))
    assert res["counts"]["prospects"] == 7 and oct(__import__("os").stat(res["file"]).st_mode)[-3:] == "600"
    with session_scope() as db:  # damage: delete everything after the backup
        db.query(Prospect).delete()
    with pytest.raises(ValueError):
        restore(res["file"])
    reset_engine()
    out = restore(res["file"], confirm=True)
    assert out["counts"]["prospects"] == 7
    reset_engine()
    with session_scope() as db:
        assert db.query(Prospect).count() == 7
    assert get_settings().database_url.startswith("sqlite")
