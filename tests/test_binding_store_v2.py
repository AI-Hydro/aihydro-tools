"""Binding store v2: many chats per study, a new file, one-time v1 import, a derived v1 projection."""
from __future__ import annotations

import json

import pytest

from binding_helpers import CHAT_A, CHAT_B, CHAT_C, make_study

from ai_hydro.session.chat_binding import ChatBindingStore, InvalidChatIdError, is_valid_chat_id


def test_many_chats_may_bind_one_study_and_none_is_stolen(tmp_path):
    store = ChatBindingStore(tmp_path / "chat_bindings.v2.json")
    store.bind(CHAT_A, "basin_x")
    store.bind(CHAT_B, "basin_x")
    assert store.lookup_study(CHAT_A) == "basin_x"
    assert store.lookup_study(CHAT_B) == "basin_x"
    assert store.lookup_chats("basin_x") == [CHAT_A, CHAT_B]
    assert store.lookup_chat("basin_x") == CHAT_B          # v1 accessor: the most recent


def test_a_chat_has_one_study_and_rebinding_replaces_it(tmp_path):
    store = ChatBindingStore(tmp_path / "b.json")
    store.bind(CHAT_A, "old")
    store.bind(CHAT_A, "new")
    assert store.lookup_study(CHAT_A) == "new"
    assert store.lookup_chats("old") == []


def test_bind_reports_whether_the_binding_changed(tmp_path):
    store = ChatBindingStore(tmp_path / "b.json")
    assert store.bind(CHAT_A, "s") is True
    assert store.bind(CHAT_A, "s") is False


def test_a_study_id_is_not_a_chat_id(tmp_path):
    store = ChatBindingStore(tmp_path / "b.json")
    with pytest.raises(InvalidChatIdError):
        store.bind("basin_24p203_75p746", "basin_24p203_75p746")
    with pytest.raises(InvalidChatIdError):
        store.bind("01031500", "01031500")


@pytest.mark.parametrize("bad", ["chat-001", "basin_24p203_75p746", "", "01KV1EPM88P9FV5CWNVJ9V60J", "01KV1EPM88P9FV5CWNVJ9V60JYY",
                                 "01KV1EPM88P9FV5CWNVJ9V60JU"])
def test_only_a_ulid_binds(tmp_path, bad):
    store = ChatBindingStore(tmp_path / "b.json")
    with pytest.raises(ValueError):
        store.bind(bad, "some_study")
    assert not store.all_bindings()


def test_ulid_check_accepts_the_live_ids():
    assert all(is_valid_chat_id(c) for c in (CHAT_A, CHAT_B, CHAT_C, CHAT_A.lower()))


def test_unbind_study_unbinds_every_chat(tmp_path):
    store = ChatBindingStore(tmp_path / "b.json")
    store.bind(CHAT_A, "s")
    store.bind(CHAT_B, "s")
    assert store.unbind_study("s") is True
    assert store.all_bindings() == []
    assert store.unbind_study("s") is False


def test_v2_goes_to_a_new_file_and_leaves_the_v1_file_to_the_projection(tmp_path):
    store = ChatBindingStore(tmp_path / "chat_bindings.v2.json")
    store.bind(CHAT_A, "s")
    data = json.loads((tmp_path / "chat_bindings.v2.json").read_text())
    assert data["schema"] == 2 and data["bindings"][CHAT_A]["study_id"] == "s"
    assert "study_to_chat" not in data                      # no stored inverse


# ---------------------------------------------------------------- v1 import

V1 = {
    "chat_to_study": {CHAT_A: "usgs_01031500", CHAT_B: "01031500", "basin_24p203_75p746": "basin_24p203_75p746"},
    "study_to_chat": {"usgs_01031500": CHAT_A, "01031500": CHAT_B, "basin_24p203_75p746": "basin_24p203_75p746"},
    "meta": {},
}


def test_v1_is_imported_once_with_a_backup_and_junk_is_dropped(tmp_path):
    (tmp_path / "chat_studies.json").write_text(json.dumps(V1))
    store = ChatBindingStore(tmp_path / "chat_bindings.v2.json")
    assert store.lookup_study(CHAT_A) == "usgs_01031500"
    assert store.lookup_study(CHAT_B) == "01031500"
    assert store.lookup_study("basin_24p203_75p746") is None      # a study id used as a chat id
    assert (tmp_path / "chat_studies.json.v1.bak").exists()
    assert json.loads((tmp_path / "chat_studies.json.v1.bak").read_text()) == V1
    meta = json.loads((tmp_path / "chat_bindings.v2.json").read_text())["meta"]
    assert meta["imported_v1_entries"] == 2 and meta["imported_v1_skipped"] == 1


def test_the_v1_file_is_never_read_after_the_import(tmp_path):
    (tmp_path / "chat_studies.json").write_text(json.dumps(V1))
    store = ChatBindingStore(tmp_path / "chat_bindings.v2.json")
    store.lookup_study(CHAT_A)                                      # triggers the import
    # a 2.3.7-shaped write to the v1 file afterwards ...
    (tmp_path / "chat_studies.json").write_text(json.dumps({
        "chat_to_study": {CHAT_A: "stolen_by_2_3_7", CHAT_C: "late"}, "study_to_chat": {}, "meta": {}}))
    # ... changes nothing v2 resolves
    store2 = ChatBindingStore(tmp_path / "chat_bindings.v2.json")
    assert store2.lookup_study(CHAT_A) == "usgs_01031500"
    assert store2.lookup_study(CHAT_C) is None


def test_the_backup_is_the_original_even_after_later_projections(tmp_path):
    (tmp_path / "chat_studies.json").write_text(json.dumps(V1))
    store = ChatBindingStore(tmp_path / "chat_bindings.v2.json")
    store.bind(CHAT_C, "another")
    assert json.loads((tmp_path / "chat_studies.json.v1.bak").read_text()) == V1


# -------------------------------------------------------------- v1 projection

def test_the_derived_v1_projection_holds_active_studies_only(tmp_path):
    make_study("live")
    make_study("gone", archived=True)
    store = ChatBindingStore(tmp_path / "chat_bindings.v2.json")
    store.bind(CHAT_A, "live")
    store.bind(CHAT_B, "gone")
    store.bind(CHAT_C, "never_existed")
    v1 = json.loads((tmp_path / "chat_studies.json").read_text())
    assert v1["chat_to_study"] == {CHAT_A: "live"}
    assert v1["study_to_chat"] == {"live": CHAT_A}
    assert set(v1) == {"chat_to_study", "study_to_chat", "meta"}   # the shape the 0.4.0 extension reads


def test_two_chats_on_one_study_both_appear_in_the_projection(tmp_path):
    make_study("s")
    store = ChatBindingStore(tmp_path / "chat_bindings.v2.json")
    store.bind(CHAT_A, "s")
    store.bind(CHAT_B, "s")
    v1 = json.loads((tmp_path / "chat_studies.json").read_text())
    assert v1["chat_to_study"] == {CHAT_A: "s", CHAT_B: "s"}


def test_a_corrupt_v2_file_is_preserved_and_never_overwritten(tmp_path):
    p = tmp_path / "b.json"
    p.write_text('{"schema": 2, "bindings": {"01KV1EPM88P9FV5CWNVJ9V60JY": ')   # truncated mid-write
    store = ChatBindingStore(p)
    assert store.lookup_study(CHAT_A) is None
    assert p.read_text().startswith('{"schema": 2')                              # reads do not write
    store.bind(CHAT_B, "s")
    kept = list(tmp_path.glob("b.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text().startswith('{"schema": 2')
    assert store.lookup_study(CHAT_B) == "s"


def _worker(args):
    path, worker, n = args
    from ai_hydro.session.chat_binding import ChatBindingStore

    store = ChatBindingStore(__import__("pathlib").Path(path))
    for i in range(n):
        store.bind(f"01H{worker:02d}{i:02d}".ljust(26, "0"), f"study_{worker}_{i}")


def test_concurrent_processes_lose_no_binding(tmp_path):
    import multiprocessing as mp

    path = tmp_path / "chat_bindings.v2.json"
    ChatBindingStore(path)
    ctx = mp.get_context("spawn")
    with ctx.Pool(4) as pool:
        pool.map(_worker, [(str(path), w, 20) for w in range(4)])
    assert len(ChatBindingStore(path).all_bindings()) == 80
    assert not list(tmp_path.glob("*.corrupt-*"))
