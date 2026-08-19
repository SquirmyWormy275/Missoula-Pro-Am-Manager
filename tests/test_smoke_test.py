import pytest

from scripts import smoke_test


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"status": "ok", "db": True, "migration_current": True,
          "migration_head": "head", "migration_rev": "head"}, None),
        ({"status": "degraded", "db": True, "migration_current": True},
         "status is not ok"),
        ({"status": "ok", "db": False, "migration_current": True},
         "database is not healthy"),
        ({"status": "ok", "db": True, "migration_current": False},
         "migration is not current"),
        ({"status": "ok", "db": True, "migration_current": True,
          "migration_head": "new", "migration_rev": "old"},
         "migration revision does not match head"),
        ({"status": "ok", "db": True, "migration_current": True,
          "migration_head": None, "migration_rev": None},
         "migration head is missing"),
        ([], "health response JSON is not an object"),
    ],
)
def test_validate_health_payload(payload, expected_error):
    assert smoke_test.validate_health_payload(payload) == expected_error


def test_validate_health_payload_checks_expected_version_and_migration():
    payload = {
        "status": "ok",
        "db": True,
        "migration_current": True,
        "migration_head": "abc123",
        "migration_rev": "abc123",
        "version": "2.14.16",
        "git_commit": "a" * 40,
    }

    assert smoke_test.validate_health_payload(
        payload,
        expected_version="2.14.16",
        expected_migration="abc123",
        expected_commit="a" * 40,
    ) is None
    assert "version" in smoke_test.validate_health_payload(
        payload, expected_version="2.14.17"
    )
    assert "migration" in smoke_test.validate_health_payload(
        payload, expected_migration="different"
    )
    assert "commit" in smoke_test.validate_health_payload(
        payload, expected_commit="b" * 40
    ).lower()


def test_same_origin_allows_paths_but_rejects_host_scheme_and_port_changes():
    base = "https://example.test"

    assert smoke_test.same_origin(base, "https://example.test/portal/")
    assert smoke_test.same_origin(base, "/portal/spectator/2")
    assert not smoke_test.same_origin(base, "http://example.test/portal/")
    assert not smoke_test.same_origin(base, "https://other.test/portal/")
    assert not smoke_test.same_origin(base, "https://example.test:444/portal/")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.test/portal/spectator/2?view=desktop", 2),
        ("https://example.test/portal/spectator/991", 991),
        ("https://example.test/portal/", None),
        ("https://example.test/portal/spectator/nope", None),
    ],
)
def test_extract_spectator_tournament_id(url, expected):
    assert smoke_test.extract_spectator_tournament_id(url) == expected


def test_validate_public_standings_requires_the_endpoint_contract():
    valid = (
        '{"tournament":{"id":2},"teams":[],"bull":[],"belle":[],'
        '"pro_earnings":[]}'
    )
    assert smoke_test.validate_public_standings(
        valid,
        "application/json",
        2,
    ) is None
    assert smoke_test.validate_public_standings(
        "<html>login</html>",
        "text/html",
        2,
    ) == "response is not JSON"
    assert smoke_test.validate_public_standings(
        '{"error": "database unavailable"}',
        "application/json",
        2,
    ) == "response contains an error"
    assert "teams" in smoke_test.validate_public_standings(
        '{"tournament":{"id":2}}', "application/json", 2
    )
    assert "tournament" in smoke_test.validate_public_standings(
        valid, "application/json", 3
    )
