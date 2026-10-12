"""python -m shots.db: upgrade / current / status against SHOTS_DB."""

import pytest

from shots.db import migrate
from shots.db.__main__ import main
from shots.db.engine import db_path


def test_current_does_not_create_the_db(capsys):
    main(["current"])
    assert "(none)" in capsys.readouterr().out and not db_path().exists()
    with pytest.raises(SystemExit):
        main(["status"])
    assert not db_path().exists()


def test_upgrade_then_current_and_status(capsys):
    main(["upgrade"])
    assert db_path().exists() and migrate.is_current(db_path())
    main(["current"])
    assert f"revision {migrate.head()}" in capsys.readouterr().out
    main(["status"])
    assert '"matches_played": 0' in capsys.readouterr().out
