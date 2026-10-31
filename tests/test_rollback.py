import xml.etree.ElementTree as ET

import pytest

from bus_sync.rollback import (Element, RollbackConflict, RollbackRefused, build_osmchange, execute_revert,
                               parse_history, parse_osmchange, plan_revert)

BOT = 42


def node(i, v, tags=(), visible=True, lat="37.5", lon="127.0"):
    return Element("node", i, v, visible, tuple(tags), lat, lon)


class FakeApi:
    def __init__(self, changes, history, uid=BOT, open_=False, upload_error=None):
        self.changes, self.histories, self.uid, self.open = changes, history, uid, open_
        self.upload_error = upload_error
        self.uploaded, self.closed, self.created = None, [], []

    def changeset(self, cid):
        return {"uid": self.uid, "open": self.open}

    def download(self, cid):
        return self.changes

    def history(self, kind, i):
        return self.histories[f"{kind}/{i}"]

    def create_changeset(self, tags):
        self.created.append(tags)
        return 900

    def upload(self, cid, data):
        if self.upload_error:
            raise self.upload_error
        self.uploaded = data

    def close(self, cid):
        self.closed.append(cid)


def scenario():
    stop_v1 = node(1, 1, [("name", "시청")])
    stop_v2 = node(1, 2, [("name", "시청앞")])
    way_v3 = Element("way", 5, 3, True, (("highway", "bus_stop"),), nodes=(1, 2))
    way_v4 = Element("way", 5, 4, True, (("highway", "platform"),), nodes=(1, 2))
    new_node = node(9, 1, [("highway", "bus_stop")])
    gone_v1 = node(7, 1, [("name", "폐지")])
    gone_v2 = node(7, 2, visible=False, lat=None, lon=None)
    changes = [("modify", stop_v2), ("modify", way_v4), ("create", new_node), ("delete", gone_v2)]
    history = {"node/1": [stop_v1, stop_v2], "way/5": [way_v3, way_v4], "node/9": [new_node], "node/7": [gone_v1, gone_v2]}
    return changes, history


def test_revert_restores_previous_versions_undeletes_and_deletes_creations_atomically():
    api = FakeApi(*scenario())
    plan = plan_revert(api, 100, BOT)
    assert plan.conflicts == []
    assert [e.key for e in plan.restore] == ["node/1", "node/7", "way/5"]
    assert [e.key for e in plan.delete] == ["node/9"]
    assert execute_revert(api, plan, "Revert 100") == 900
    assert api.closed == [900] and api.created[0]["bot"] == "yes"
    root = ET.fromstring(api.uploaded)
    modify, delete = root.find("modify"), root.find("delete")
    assert [(e.tag, e.get("id"), e.get("version")) for e in modify] == [("node", "1", "2"), ("node", "7", "2"), ("way", "5", "4")]
    assert modify[0].find("tag").get("v") == "시청"  # previous name restored on the current version
    assert modify[1].get("lat") == "37.5"  # undeleted with its last visible position
    assert [nd.get("ref") for nd in modify[2].findall("nd")] == ["1", "2"]
    assert [(e.tag, e.get("id"), e.get("version")) for e in delete] == [("node", "9", "1")]


def test_later_edit_by_someone_else_blocks_the_whole_upload():
    changes, history = scenario()
    history["node/1"].append(node(1, 3, [("name", "시청 (수정)")]))
    api = FakeApi(changes, history)
    plan = plan_revert(api, 100, BOT)
    assert plan.conflicts == [{"element": "node/1", "reason": "edited_after_bot", "bot_version": 2, "current_version": 3}]
    with pytest.raises(RollbackConflict):
        execute_revert(api, plan, "Revert 100")
    assert api.created == [] and api.uploaded is None


def test_multiple_touches_revert_to_before_the_first_one():
    v1, v2, v3 = node(3, 1, [("a", "1")]), node(3, 2, [("a", "2")]), node(3, 3, [("a", "3")])
    plan = plan_revert(FakeApi([("modify", v2), ("modify", v3)], {"node/3": [v1, v2, v3]}), 1, BOT)
    assert plan.restore == [Element("node", 3, 3, True, (("a", "1"),), "37.5", "127.0")]


@pytest.mark.parametrize("api", [FakeApi([], {}, uid=7), FakeApi([], {}, open_=True)])
def test_refuses_other_accounts_and_open_changesets(api):
    with pytest.raises(RollbackRefused):
        plan_revert(api, 1, BOT)


def test_server_side_conflict_still_closes_the_revert_changeset():
    api = FakeApi(*scenario(), upload_error=RollbackConflict("OSM 412: node still used"))
    with pytest.raises(RollbackConflict):
        execute_revert(api, plan_revert(api, 100, BOT), "Revert 100")
    assert api.closed == [900]


def test_parses_osm_api_xml():
    change = parse_osmchange(b'<osmChange><modify><node id="1" version="2" lat="1" lon="2"><tag k="a" v="b"/></node></modify>'
                             b'<delete><way id="5" version="3" visible="false"/></delete></osmChange>')
    assert [(a, e.key, e.version, e.tags) for a, e in change] == [("modify", "node/1", 2, (("a", "b"),)), ("delete", "way/5", 3, ())]
    history = parse_history(b'<osm><relation id="8" version="2" visible="false"/><relation id="8" version="1">'
                            b'<member type="way" ref="5" role="platform"/></relation></osm>')
    assert [(e.version, e.visible, e.members) for e in history] == [(1, True, (("way", 5, "platform"),)), (2, False, ())]
    assert b"<osmChange" in build_osmchange(plan_revert(FakeApi(*scenario()), 1, BOT), 5)
