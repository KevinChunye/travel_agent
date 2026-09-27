from datetime import datetime, timezone
import json
import pytest
from pydantic import ValidationError
from src.cli import main
from src.services.planning import PlanCheckpoint, PlanningStore
from src.storage.repository import SQLiteRepository


def test_checkpoint_survives_restart(tmp_path, trip):
    path = tmp_path / "state.sqlite3"
    repo = SQLiteRepository(path)
    repo.save_trip(trip)
    PlanningStore(repo).save(trip.id, PlanCheckpoint(phase="blocked", next_action="Ask to retry research", unresolved=["timeout"]))
    repo.close()
    repo = SQLiteRepository(path)
    store = PlanningStore(repo)
    assert store.get(trip.id).unresolved == ["timeout"]
    assert [e["phase"] for e in store.events(trip.id)] == ["blocked"]
    repo.close()


def test_ready_requires_parent_review():
    with pytest.raises(ValidationError):
        PlanCheckpoint(phase="ready", next_action="Deliver")
    with pytest.raises(ValidationError):
        PlanCheckpoint(phase="ready", next_action="Deliver", reviewed=True, unresolved=["No citation"])


def test_bad_child_cannot_overwrite_checkpoint(repo, trip):
    store = PlanningStore(repo)
    store.save(trip.id, PlanCheckpoint(phase="researching", next_action="Await child"))
    with pytest.raises(ValidationError):
        candidate = PlanCheckpoint(phase="review", next_action="Review", research=[{
            "category":"hotel", "name":"Example", "source_url":"javascript:alert(1)",
            "checked_at":datetime.now(timezone.utc), "rationale":"near station", "caveats":"unknown price"}])
        store.save(trip.id, candidate)
    assert store.get(trip.id).phase == "researching"
    assert len(store.events(trip.id)) == 1


def test_memory_defaults_and_explicit_override(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("TRAVEL_PROVIDERS", "mock")
    main(["prefs-set", "--user", "demo", "--json", json.dumps({"home_airport":"BOS", "cabin_preference":"business", "dietary_preferences":["vegetarian"]})])
    capsys.readouterr()
    main(["new-trip", "--user", "demo", "--request-json", '{"destination":"JFK","outbound_date":{"start":"2027-10-09"}}'])
    first = json.loads(capsys.readouterr().out)
    repo = SQLiteRepository(tmp_path / "memory.sqlite3")
    assert repo.get_trip(first["trip_id"]).request.origin == "BOS"
    assert repo.get_trip(first["trip_id"]).request.cabin.value == "business"
    main(["new-trip", "--user", "demo", "--request-json", '{"origin":"ORD","cabin":"economy"}'])
    second = json.loads(capsys.readouterr().out)
    assert repo.get_trip(second["trip_id"]).request.origin == "ORD"
    assert repo.get_trip(second["trip_id"]).request.cabin.value == "economy"
    assert repo.get_preferences("demo").dietary_preferences == ["vegetarian"]
    assert repo.get_preferences("other") is None
    repo.close()


def test_legacy_purchase_absent():
    from src.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(["book", "--trip", "does-not-exist"])


def test_ready_requires_findings_and_failure_preserves_them(repo, trip):
    with pytest.raises(ValidationError, match='cited research'):
        PlanCheckpoint(phase='ready', next_action='Done', reviewed=True)
    store = PlanningStore(repo)
    finding = {'category':'transport', 'name':'Synthetic train',
               'source_url':'https://example.org/transport',
               'checked_at':datetime.now(timezone.utc),
               'rationale':'Fits the synthetic route', 'caveats':'Test data only'}
    store.save(trip.id, PlanCheckpoint(phase='ready', next_action='Review options',
                                      reviewed=True, research=[finding]))
    store.save(trip.id, PlanCheckpoint(phase='researching', next_action='Await refresh'))
    assert store.get(trip.id).research[0].name == 'Synthetic train'
    store.save(trip.id, PlanCheckpoint(phase='blocked', next_action='Ask to retry',
                                      unresolved=['Timed out']))
    assert store.get(trip.id).research[0].name == 'Synthetic train'
    assert store.get(trip.id).phase == 'blocked'
