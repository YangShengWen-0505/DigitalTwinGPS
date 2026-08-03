from digital_twin import db


def _plan():
    eta = db.utc_now()
    return {
        "initial_google_eta": eta,
        "routes": [{"steps": [{"points": [[25.0, 121.0], [25.1, 121.1]]}]}],
    }


def test_create_and_claim_mission_uses_sqlite_transaction(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    mission_id = db.create_mission(
        {"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan()
    )
    claimed = db.claim_next("test-worker")
    assert claimed["id"] == mission_id
    assert claimed["status"] == "running"
    assert claimed["planned_route_points"] == 2
    assert db.claim_next("other-worker") is None


def test_unplanned_mission_is_claimed_in_planning_state(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "planning.sqlite3")
    db.init_db()
    mission_id = db.create_mission({
        "init_loc": "25,121", "stops": [{"name": "A", "mode": "walking"}],
    })
    claimed = db.claim_next("test-worker")
    assert claimed["id"] == mission_id
    assert claimed["status"] == "planning"
    assert claimed["route_revision"] == 0


def test_worker_lock_uses_epoch_and_allows_only_one_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    future = db.utc_epoch_ms() + 30_000
    assert db.acquire_worker("one", future)
    assert not db.acquire_worker("two", future)
    db.release_worker("one")
    assert db.acquire_worker("two", future)


def test_running_mission_becomes_interrupted_after_owner_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    mission_id = db.create_mission(
        {"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan()
    )
    db.claim_next("test-worker")
    db.init_db(interrupt_running=True)
    assert db.get_mission(mission_id)["status"] == "interrupted"


def test_cancel_query_does_not_decode_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    mission_id = db.create_mission(
        {"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan()
    )
    assert db.is_cancel_requested(mission_id) is False
    metadata = db.get_mission(mission_id, include_plan=False, include_payload=False)
    assert "plan" not in metadata
    assert "payload" not in metadata


def test_new_mission_only_cancels_active_completed_holder(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    first = db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan())
    db.update_mission(first, status="completed", cancel_requested=0, is_holding_final_position=0)
    second = db.create_mission({"init_loc": "25,121", "stops": [{"name": "B"}]}, _plan())
    assert db.get_mission(first)["cancel_requested"] is False
    assert db.get_mission(second)["status"] == "queued"
