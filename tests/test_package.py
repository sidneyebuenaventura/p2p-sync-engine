import p2p_sync


def test_package_exposes_version() -> None:
    assert p2p_sync.__version__ == "0.1.0"
