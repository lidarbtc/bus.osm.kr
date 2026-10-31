"""Revert changesets made by this project's bot account.

A revert is planned from the changeset's own osmChange and each element's
history. Nothing is uploaded if any element was edited after the bot, if the
changeset belongs to another account, or if it is still open. All reverted
elements go into one changeset, which OSM applies atomically.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace

import requests

OSM_API = "https://api.openstreetmap.org/api/0.6"
ORDER = {"node": 0, "way": 1, "relation": 2}
USER_AGENT = "osm-korea-bus-sync/0.1 (rollback)"


class RollbackRefused(RuntimeError):
    """The request is outside what this tool may revert."""


class RollbackConflict(RuntimeError):
    """OSM rejected the revert because the data changed; needs a person."""


class RollbackUnavailable(RuntimeError):
    """Bot credentials are not configured on this server."""


@dataclass(frozen=True)
class Element:
    type: str
    id: int
    version: int
    visible: bool = True
    tags: tuple = ()
    lat: str | None = None
    lon: str | None = None
    nodes: tuple = ()
    members: tuple = ()

    @property
    def key(self) -> str:
        return f"{self.type}/{self.id}"


def parse_element(node: ET.Element) -> Element:
    return Element(
        type=node.tag, id=int(node.get("id")), version=int(node.get("version")),
        visible=node.get("visible", "true") == "true",
        tags=tuple((t.get("k"), t.get("v")) for t in node.findall("tag")),
        lat=node.get("lat"), lon=node.get("lon"),
        nodes=tuple(int(n.get("ref")) for n in node.findall("nd")),
        members=tuple((m.get("type"), int(m.get("ref")), m.get("role", "")) for m in node.findall("member")),
    )


def parse_osmchange(data: bytes) -> list[tuple[str, Element]]:
    root = ET.fromstring(data)
    return [(block.tag, parse_element(item)) for block in root if block.tag in {"create", "modify", "delete"}
            for item in block if item.tag in ORDER]


def parse_history(data: bytes) -> list[Element]:
    return sorted((parse_element(item) for item in ET.fromstring(data) if item.tag in ORDER), key=lambda e: e.version)


def element_xml(element: Element, changeset: int) -> ET.Element:
    node = ET.Element(element.type, id=str(element.id), version=str(element.version), changeset=str(changeset))
    if element.type == "node":
        node.set("lat", element.lat)
        node.set("lon", element.lon)
    for ref in element.nodes:
        ET.SubElement(node, "nd", ref=str(ref))
    for kind, ref, role in element.members:
        ET.SubElement(node, "member", type=kind, ref=str(ref), role=role)
    for key, value in element.tags:
        ET.SubElement(node, "tag", k=key, v=value)
    return node


@dataclass
class Plan:
    changeset_id: int
    restore: list[Element] = field(default_factory=list)
    delete: list[Element] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"changeset_id": self.changeset_id, "restore": [e.key for e in self.restore],
                "delete": [e.key for e in self.delete], "conflicts": self.conflicts}


class OsmApi:
    def __init__(self, base: str = OSM_API, token: str | None = None, session=None):
        self.base = base.rstrip("/")
        self.token = token
        self.session = session or requests.Session()

    @classmethod
    def from_env(cls) -> tuple["OsmApi", int]:
        token, uid = os.environ.get("OSM_BOT_TOKEN"), os.environ.get("OSM_BOT_UID", "")
        if not token or not uid.isdigit():
            raise RollbackUnavailable("OSM 봇 계정(OSM_BOT_TOKEN, OSM_BOT_UID)이 이 서버에 설정되어 있지 않습니다.")
        return cls(os.environ.get("OSM_API_BASE", OSM_API), token), int(uid)

    def _call(self, method: str, path: str, data: bytes | None = None, auth: bool = False) -> bytes:
        headers = {"User-Agent": USER_AGENT}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["Content-Type"] = "text/xml; charset=utf-8"
        response = self.session.request(method, self.base + path, data=data, headers=headers, timeout=(20, 180))
        if response.status_code in {409, 412}:
            raise RollbackConflict(f"OSM {response.status_code}: {response.text[:300]}")
        if response.status_code >= 400:
            raise RuntimeError(f"OSM {method} {path} failed: {response.status_code} {response.text[:300]}")
        return response.content

    def changeset(self, changeset_id: int) -> dict:
        node = ET.fromstring(self._call("GET", f"/changeset/{changeset_id}")).find("changeset")
        return {"uid": int(node.get("uid", "0")), "open": node.get("open") == "true"}

    def download(self, changeset_id: int) -> list[tuple[str, Element]]:
        return parse_osmchange(self._call("GET", f"/changeset/{changeset_id}/download"))

    def history(self, kind: str, element_id: int) -> list[Element]:
        return parse_history(self._call("GET", f"/{kind}/{element_id}/history"))

    def create_changeset(self, tags: dict) -> int:
        root = ET.Element("osm")
        changeset = ET.SubElement(root, "changeset")
        for key, value in tags.items():
            ET.SubElement(changeset, "tag", k=key, v=value)
        return int(self._call("PUT", "/changeset/create", ET.tostring(root), auth=True))

    def upload(self, changeset_id: int, osmchange: bytes) -> None:
        self._call("POST", f"/changeset/{changeset_id}/upload", osmchange, auth=True)

    def close(self, changeset_id: int) -> None:
        self._call("PUT", f"/changeset/{changeset_id}/close", auth=True)


def plan_revert(api, changeset_id: int, bot_uid: int, max_elements: int = 2000) -> Plan:
    meta = api.changeset(changeset_id)
    if meta["uid"] != bot_uid:
        raise RollbackRefused(f"변경세트 {changeset_id}는 봇 계정이 만든 것이 아닙니다.")
    if meta["open"]:
        raise RollbackRefused(f"변경세트 {changeset_id}가 아직 열려 있습니다.")
    # One changeset may touch an element several times; revert to before its first touch.
    touched: dict[str, dict] = {}
    for action, element in api.download(changeset_id):
        entry = touched.setdefault(element.key, {"element": element, "first": element.version, "last": element.version, "created": False})
        entry["first"] = min(entry["first"], element.version)
        entry["last"] = max(entry["last"], element.version)
        entry["created"] |= action == "create"
    if len(touched) > max_elements:
        raise RollbackRefused(f"변경 객체가 {len(touched)}개로 자동 롤백 한도({max_elements})를 넘습니다.")
    plan = Plan(changeset_id)
    for key, entry in sorted(touched.items(), key=lambda item: (ORDER[item[1]["element"].type], item[1]["element"].id)):
        element = entry["element"]
        history = api.history(element.type, element.id)
        current = history[-1]
        if current.version != entry["last"]:
            plan.conflicts.append({"element": key, "reason": "edited_after_bot", "bot_version": entry["last"], "current_version": current.version})
            continue
        if entry["created"]:
            if current.visible:
                plan.delete.append(current)
            continue
        previous = next((h for h in history if h.version == entry["first"] - 1), None)
        if previous is None or not previous.visible:
            plan.conflicts.append({"element": key, "reason": "no_visible_previous_version", "bot_version": entry["first"]})
            continue
        # Uploading the old content with the current version restores (or undeletes) it.
        plan.restore.append(replace(previous, version=current.version, visible=True))
    return plan


def build_osmchange(plan: Plan, changeset_id: int) -> bytes:
    root = ET.Element("osmChange", version="0.6", generator="osm-korea-bus-sync")
    if plan.restore:
        block = ET.SubElement(root, "modify")
        for element in sorted(plan.restore, key=lambda e: (ORDER[e.type], e.id)):
            block.append(element_xml(element, changeset_id))
    if plan.delete:
        block = ET.SubElement(root, "delete")
        for element in sorted(plan.delete, key=lambda e: (-ORDER[e.type], e.id)):
            block.append(element_xml(element, changeset_id))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def execute_revert(api, plan: Plan, comment: str) -> int:
    if plan.conflicts:
        raise RollbackConflict("수동 처리가 필요한 객체가 있어 업로드하지 않았습니다.")
    if not plan.restore and not plan.delete:
        raise RollbackRefused("되돌릴 변경이 없습니다.")
    changeset_id = api.create_changeset({
        "comment": comment[:255], "created_by": "osm-korea-bus-sync", "bot": "yes",
        "source": "https://github.com/lidarbtc/bus.osm.kr",
    })
    try:
        api.upload(changeset_id, build_osmchange(plan, changeset_id))
    finally:
        api.close(changeset_id)
    return changeset_id
