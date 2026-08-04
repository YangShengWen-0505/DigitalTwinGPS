from mock_gps import db


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


def test_active_missions_become_interrupted_on_container_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    running_id = db.create_mission(
        {"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan()
    )
    db.claim_next("test-worker")
    planning_id = db.create_mission(
        {"init_loc": "25,121", "stops": [{"name": "B", "mode": "walking"}]}
    )
    assert db.interrupt_active_missions() == 2
    for mission_id in (running_id, planning_id):
        mission = db.get_mission(mission_id)
        assert mission["status"] == "interrupted"
        assert mission["cancel_requested"] is True
        assert mission["finished_at"] is not None


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


def test_cancel_before_claim_finalizes_mission(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    mission_id = db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]})

    assert db.request_cancel() == mission_id
    assert db.claim_next("test-worker") is None

    mission = db.get_mission(mission_id)
    assert mission["status"] == "aborted"
    assert mission["finished_at"] is not None


def test_cancel_after_claim_leaves_finalization_to_the_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    mission_id = db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]})
    db.claim_next("test-worker")

    assert db.request_cancel() == mission_id
    mission = db.get_mission(mission_id)
    assert mission["status"] == "planning"
    assert mission["cancel_requested"] is True
    assert mission["finished_at"] is None


def test_superseded_unclaimed_mission_is_finalized(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    first = db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]})
    second = db.create_mission({"init_loc": "25,121", "stops": [{"name": "B"}]})

    assert db.get_mission(first)["status"] == "aborted"
    assert db.get_mission(first)["finished_at"] is not None
    assert db.claim_next("test-worker")["id"] == second


def test_orphaned_running_mission_is_reclaimed_on_worker_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    mission_id = db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan())
    db.claim_next("dead-worker")
    assert db.get_mission(mission_id)["status"] == "running"

    # Mirrors run_worker(): the reclaim happens once the lease has been taken.
    assert db.acquire_worker("new-worker", db.utc_epoch_ms() + 30_000)
    assert db.interrupt_active_missions("Worker restarted") == 1

    mission = db.get_mission(mission_id)
    assert mission["status"] == "interrupted"
    assert mission["finished_at"] is not None
    assert mission["worker_id"] is None


def test_finished_missions_survive_worker_startup_reclaim(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    done = db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan())
    db.update_mission(done, status="completed", cancel_requested=0, is_holding_final_position=0)

    assert db.interrupt_active_missions("Worker restarted") == 0
    assert db.get_mission(done)["status"] == "completed"


def test_new_mission_only_cancels_active_completed_holder(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    first = db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan())
    db.update_mission(first, status="completed", cancel_requested=0, is_holding_final_position=0)
    second = db.create_mission({"init_loc": "25,121", "stops": [{"name": "B"}]}, _plan())
    assert db.get_mission(first)["cancel_requested"] is False
    assert db.get_mission(second)["status"] == "queued"
