from pathlib import Path

import pytest


@pytest.fixture
def isolated_patchfox_user_config(tmp_path, monkeypatch):
    """Keep tests independent from the developer's real ~/.patchfox state.

    Some provider tests intentionally clear the whole process environment.  In
    those cases PATCHFOX_HOME disappears temporarily, so patch the user-home
    fallback as well as setting the normal override.
    """

    from patchfox.config import store

    user_home = tmp_path / "user-home"
    config_home = user_home / ".patchfox"
    monkeypatch.setattr(store, "_USER_HOME", Path(user_home))
    monkeypatch.setenv(store.CONFIG_HOME_ENV, str(config_home))
    return config_home
