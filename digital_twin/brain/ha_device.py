"""Pebble as its own Home Assistant device, the way the companion phone app is.

Registers through HA's mobile_app integration, so the robot appears under
Settings -> Devices with its own notify service (notify.mobile_app_pebble) and
sensors. Notifications are delivered over HA's WebSocket "local push" channel:
no cloud, no port opened on the robot.

    dev = HADevice("http://homeassistant.local:8123", token)
    dev.register()                       # once; reused from config/ha_device.json
    dev.listen(lambda msg, title, data: ...)
    dev.update_sensor("character", "Marvin")
    dev.entities({"light", "switch"})    # what Pebble may control, from HA itself

API references: developers.home-assistant.io/docs/api/native-app-integration
and the mobile_app/push_notification_channel WebSocket command.
"""

import json
import logging
import platform
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger("pebble.ha_device")

STATE_FILE = Path(__file__).resolve().parent.parent / "config" / "ha_device.json"
APP_ID = "nz.ac.auckland.compsys730.pebble"

# Notification messages the companion apps treat as commands, not text to speak.
CONTROL_MESSAGES = re.compile(r"^(clear_notification|remove_channel|request_location_update"
                              r"|command_\w+)$")

# Measurements worth reading out from a controllable device's own sensors
# (a smart plug's power and current, a thermostat's temperature...).
READING_CLASSES = {"power", "current", "voltage", "energy", "temperature", "humidity",
                   "battery", "illuminance", "carbon_dioxide", "pm25"}

# (domain, device_class) -> what people call it
KINDS = {
    ("switch", "outlet"): "plug", ("switch", None): "switch", ("light", None): "light",
    ("media_player", "tv"): "TV", ("media_player", "speaker"): "speaker",
    ("media_player", None): "media player", ("fan", None): "fan", ("cover", None): "blind",
    ("climate", None): "heater/thermostat", ("scene", None): "scene",
    ("input_boolean", None): "toggle",
}

SENSORS = {
    "character": {"name": "Character", "icon": "mdi:robot-happy-outline"},
    "emotion":   {"name": "Emotion",   "icon": "mdi:emoticon-outline"},
}


def parse_due(value):
    """HA due/start: "2026-10-11T22:53:00+13:00" or a bare date "2026-10-12"
    (taken as 09:00 local, a sensible time to be reminded of a day's task)."""
    from datetime import date, datetime, time as dtime
    if not value:
        return None
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), dtime(9, 0)).astimezone()
    d = datetime.fromisoformat(value)
    return d if d.tzinfo else d.astimezone()


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class HADevice:
    def __init__(self, base_url, token, name="Pebble", model="Pebble digital twin",
                 state_file=STATE_FILE):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.name = name
        self.model = model
        self.state_file = Path(state_file)
        self.webhook_id = None
        self.readings = {}      # controllable entity -> [(sensor entity, label)]
        self.reminders = None   # list[str] once fetched; refreshed by watch_reminders()
        self.items = []         # the same, structured (see fetch_items)
        self._wake = threading.Event()

    # ------------------------------------------------------------ REST

    def _post(self, path, body, auth=True):
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(body).encode(), method="POST",
            # Cloudflare in front of HA rejects urllib's default agent (error 1010)
            headers={"Content-Type": "application/json", "User-Agent": "Pebble/0.1",
                     **({"Authorization": f"Bearer {self.token}"} if auth else {})})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def _get(self, path):
        req = urllib.request.Request(self.base_url + path, headers={
            "Authorization": f"Bearer {self.token}", "User-Agent": "Pebble/0.1"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)

    def states(self, entity_ids):
        """{entity_id: "on (Current consumption 0.0 W, Voltage 237.7 V)"} right
        now, in one REST call. Readings come from the device's own sensors."""
        now = {s["entity_id"]: s for s in self._get("/api/states")}
        out = {}
        for eid in entity_ids:
            if eid not in now:
                continue
            extra = []
            for sid, label in self.readings.get(eid, []):
                st = now.get(sid)
                if st and st["state"] not in ("unknown", "unavailable"):
                    unit = st["attributes"].get("unit_of_measurement") or ""
                    extra.append(f"{label} {st['state']} {unit}".strip())
            out[eid] = now[eid]["state"] + (f" ({', '.join(extra)})" if extra else "")
        return out

    # ------------------------------------------------------- reminders

    def fetch_items(self, hours=24):
        """Open to-do items (any list) and the next `hours` of calendar events:
        [{"list", "list_name", "uid", "summary", "note", "due", "done_ok"}].
        `due` is an aware datetime or None; `done_ok` = can be ticked off."""
        from datetime import datetime, timedelta, timezone
        items = []
        ws = self._ws_open()
        try:
            states = self._ws_call(ws, 1, "get_states")
            todos = [s for s in states if s["entity_id"].startswith("todo.")]
            for i, t in enumerate(todos):
                for it in self._ws_call(ws, 10 + i, "todo/item/list",
                                        entity_id=t["entity_id"]).get("items", []):
                    if it.get("status") != "needs_action":
                        continue
                    items.append({
                        "list": t["entity_id"],
                        "list_name": t["attributes"].get("friendly_name", t["entity_id"]),
                        "uid": it.get("uid") or it["summary"], "summary": it["summary"],
                        "note": it.get("description") or "", "due": parse_due(it.get("due")),
                        "done_ok": True})
        finally:
            ws.close()
        now = datetime.now(timezone.utc)
        try:
            calendars = self._get("/api/calendars")
        except urllib.error.HTTPError as e:
            if e.code != 404:                         # 404: no calendar integration
                raise
            calendars = []
        for cal in calendars:
            q = (f"?start={now.isoformat()}&end={(now + timedelta(hours=hours)).isoformat()}"
                 .replace("+", "%2B"))
            for ev in self._get(f"/api/calendars/{cal['entity_id']}{q}"):
                when = ev["start"].get("dateTime") or ev["start"].get("date")
                items.append({
                    "list": cal["entity_id"], "list_name": cal["name"],
                    "uid": f"{cal['entity_id']}|{when}|{ev['summary']}",
                    "summary": ev["summary"], "note": ev.get("description") or "",
                    "due": parse_due(when), "done_ok": False})
        return items

    @staticmethod
    def describe(item):
        """One line for the model. The [list] lets it tick a to-do item off."""
        due = f" (due {item['due'].astimezone():%a %H:%M})" if item["due"] else ""
        note = f" - {item['note']}" if item["note"] else ""
        where = f" [{item['list']}]" if item["done_ok"] else ""
        return f"{item['list_name']}{where}: {item['summary']}{due}{note}"

    def fetch_reminders(self, limit=12):
        """Lines for the model. [] means HA really has none."""
        self.items = self.fetch_items()
        return [self.describe(i) for i in self.items][:limit]

    def complete_item(self, todo_entity, item):
        """Tick a to-do item off (todo.update_item -> completed)."""
        req = urllib.request.Request(
            self.base_url + "/api/services/todo/update_item", method="POST",
            data=json.dumps({"entity_id": todo_entity, "item": item,
                             "status": "completed"}).encode(),
            headers={"Authorization": f"Bearer {self.token}", "User-Agent": "Pebble/0.1",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):
            pass
        self.poke()

    def add_item(self, todo_entity, item, due=None, description=""):
        """Add a to-do item; `due` an aware datetime (HA stores it in its own zone)."""
        body = {"entity_id": todo_entity, "item": item}
        if due is not None:
            body["due_datetime"] = due.isoformat(timespec="seconds")
        if description:
            body["description"] = description
        req = urllib.request.Request(
            self.base_url + "/api/services/todo/add_item", method="POST",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "User-Agent": "Pebble/0.1",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):
            pass
        self.poke()

    def todo_lists(self):
        return {s["entity_id"]: s["attributes"].get("friendly_name", s["entity_id"])
                for s in self._get("/api/states") if s["entity_id"].startswith("todo.")}

    def poke(self):
        """Re-read reminders now (after a change) instead of at the next poll."""
        self._wake.set()

    def watch_reminders(self, on_due=None, every=60, repeat_min=10, max_times=3,
                        stale_hours=12):
        """Keep self.reminders fresh and, when an item falls due, call
        on_due(item, times_announced). Repeats every `repeat_min` minutes until
        it's done or announced `max_times`; never announces items overdue by
        more than `stale_hours` (so a restart does not replay yesterday)."""
        from datetime import datetime, timedelta, timezone
        announced = {}                                # uid -> (times, last datetime)

        def loop():
            while True:
                now = datetime.now(timezone.utc)
                try:
                    self.reminders = self.fetch_reminders()
                except Exception as e:  # noqa: BLE001 - keep the last good list
                    log.warning("could not read reminders (%s)", e)
                wait = every
                for it in getattr(self, "items", []):
                    due = it["due"]
                    if due is None:
                        continue
                    if due > now:                     # wake up right when it falls due
                        wait = min(wait, max(1.0, (due - now).total_seconds()))
                        continue
                    if now - due > timedelta(hours=stale_hours) or on_due is None:
                        continue
                    times, last = announced.get(it["uid"], (0, None))
                    if times >= max_times:
                        continue
                    if last and now - last < timedelta(minutes=repeat_min):
                        wait = min(wait, (last + timedelta(minutes=repeat_min)
                                          - now).total_seconds())
                        continue
                    announced[it["uid"]] = (times + 1, now)
                    try:
                        on_due(it, times)
                    except Exception as e:  # noqa: BLE001
                        log.warning("reminder announce failed: %s", e)
                    if times + 1 < max_times:
                        wait = min(wait, repeat_min * 60)
                self._wake.wait(wait)
                self._wake.clear()

        threading.Thread(target=loop, daemon=True, name="ha-reminders").start()

    def _webhook(self, kind, data=None):
        body = {"type": kind}
        if data is not None:
            body["data"] = data
        return self._post(f"/api/webhook/{self.webhook_id}", body, auth=False)

    # ---------------------------------------------------- registration

    @property
    def notify_service(self):
        return f"notify.mobile_app_{slug(self.name)}"

    def register(self):
        """Register once and keep the webhook in config/ha_device.json, so every
        restart is the same device in HA rather than a new duplicate."""
        saved = {}
        if self.state_file.exists():
            saved = json.loads(self.state_file.read_text())
        if saved.get("webhook_id") and saved.get("base_url") == self.base_url:
            self.webhook_id = saved["webhook_id"]
            try:
                self._webhook("get_config")
                log.info("HA device %r reused (%s)", self.name, self.notify_service)
                self._register_sensors()
                return False
            except urllib.error.HTTPError as e:
                if e.code != 410:                     # 410 = deleted in HA; re-register
                    raise
                log.warning("HA device was deleted in Home Assistant; registering again")

        device_id = saved.get("device_id") or uuid.uuid4().hex
        reg = self._post("/api/mobile_app/registrations", {
            "device_id": device_id,
            "app_id": APP_ID,
            "app_name": "Pebble",
            "app_version": "0.1",
            "device_name": self.name,
            "manufacturer": "COMPSYS 730 Group 5",
            "model": self.model,
            "os_name": platform.system(),
            "os_version": platform.release(),
            "supports_encryption": False,
            "app_data": {"push_websocket_channel": True},
        })
        self.webhook_id = reg["webhook_id"]
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({
            "base_url": self.base_url, "device_id": device_id,
            "webhook_id": self.webhook_id, "name": self.name}, indent=2))
        self.state_file.chmod(0o600)                  # the webhook id is a credential
        log.info("registered HA device %r -> %s", self.name, self.notify_service)
        self._register_sensors()
        return True

    def _register_sensors(self):
        for uid, s in SENSORS.items():
            self._webhook("register_sensor", {
                "unique_id": uid, "name": s["name"], "type": "sensor",
                "icon": s["icon"], "state": "unknown"})

    def update_sensor(self, uid, state, attributes=None):
        """Never raises: a flaky HA must not stall the robot."""
        try:
            self._webhook("update_sensor_states", [{
                "unique_id": uid, "type": "sensor", "state": state,
                "icon": SENSORS[uid]["icon"], **({"attributes": attributes} if attributes else {})}])
        except Exception as e:  # noqa: BLE001
            log.warning("sensor %s update failed: %s", uid, e)

    # ------------------------------------------------------- websocket

    def _ws_url(self):
        return re.sub(r"^http", "ws", self.base_url) + "/api/websocket"

    def _ws_open(self):
        from websockets.sync.client import connect
        ws = connect(self._ws_url(), open_timeout=10, max_size=None)
        hello = json.loads(ws.recv())
        if hello.get("type") != "auth_required":
            raise ConnectionError(f"unexpected HA greeting: {hello}")
        ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        res = json.loads(ws.recv())
        if res.get("type") != "auth_ok":
            raise PermissionError(f"HA rejected the token: {res.get('message', res)}")
        return ws

    def _ws_call(self, ws, msg_id, kind, **kw):
        ws.send(json.dumps({"id": msg_id, "type": kind, **kw}))
        while True:
            res = json.loads(ws.recv())
            if res.get("id") == msg_id and res.get("type") == "result":
                if not res.get("success"):
                    raise RuntimeError(f"{kind}: {res.get('error')}")
                return res.get("result")

    def listen(self, on_notification):
        """Receive notify.mobile_app_<name> messages in a background thread.
        on_notification(message, title, data). Reconnects forever."""
        threading.Thread(target=self._listen_loop, args=(on_notification,),
                         daemon=True, name="ha-push").start()

    def _listen_loop(self, on_notification):
        delay = 2
        while True:
            try:
                ws = self._ws_open()
                ws.send(json.dumps({"id": 1, "type": "mobile_app/push_notification_channel",
                                    "webhook_id": self.webhook_id, "support_confirm": True}))
                log.info("listening for %s", self.notify_service)
                delay, next_id = 2, 2
                for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "result" and msg.get("id") == 1 \
                            and not msg.get("success"):
                        raise RuntimeError(f"push channel refused: {msg.get('error')}")
                    if msg.get("type") != "event":
                        continue
                    ev = msg.get("event") or {}
                    if ev.get("hass_confirm_id"):     # tell HA it was delivered
                        ws.send(json.dumps({
                            "id": next_id, "type": "mobile_app/push_notification_confirm",
                            "webhook_id": self.webhook_id,
                            "confirm_id": ev["hass_confirm_id"]}))
                        next_id += 1
                    text = (ev.get("message") or "").strip()
                    if text and not CONTROL_MESSAGES.match(text):
                        on_notification(text, ev.get("title") or "", ev.get("data") or {})
            except PermissionError as e:
                log.error("%s - push notifications disabled", e)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("HA push channel down (%s); retrying in %d s", e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)

    # -------------------------------------------------------- entities

    def entities(self, domains, max_entities=80):
        """{entity_id: "Friendly name (Area)"} for what Pebble may control.

        Uses the entities exposed to voice assistants in HA (Settings -> Voice
        assistants -> Expose), so who controls what is decided in HA's own UI.
        If nothing is exposed, falls back to every entity in `domains`."""
        ws = self._ws_open()
        try:
            states = self._ws_call(ws, 1, "get_states")
            ents = {e["entity_id"]: e for e in self._ws_call(ws, 2, "config/entity_registry/list")}
            devs = {d["id"]: d for d in self._ws_call(ws, 3, "config/device_registry/list")}
            areas = {a["area_id"]: a["name"]
                     for a in self._ws_call(ws, 4, "config/area_registry/list")}
            try:
                exp = self._ws_call(ws, 5, "homeassistant/expose_entity/list")
                exposed = {eid for eid, a in exp.get("exposed_entities", {}).items()
                           if a.get("conversation")}
            except RuntimeError:                       # older HA without exposure
                exposed = set()
        finally:
            ws.close()

        by_id = {st["entity_id"]: st for st in states}
        out, readings = {}, {}
        for st in states:
            eid = st["entity_id"]
            if eid.split(".", 1)[0] not in domains or st.get("state") == "unavailable":
                continue
            if exposed and eid not in exposed:
                continue
            reg = ents.get(eid, {})
            if reg.get("disabled_by") or reg.get("hidden_by") or reg.get("entity_category"):
                continue
            area_id = reg.get("area_id") or devs.get(reg.get("device_id"), {}).get("area_id")
            attrs = st.get("attributes", {})
            name = attrs.get("friendly_name") or eid
            # say what it IS, so "the plug" finds a switch HA knows is an outlet
            kind = KINDS.get((eid.split(".", 1)[0], attrs.get("device_class")),
                             KINDS.get((eid.split(".", 1)[0], None), eid.split(".", 1)[0]))
            out[eid] = f"{name} - {kind}" + (f", in {areas[area_id]}" if area_id in areas else "")
            dev_id = reg.get("device_id")
            dev_name = (devs.get(dev_id, {}).get("name_by_user")
                        or devs.get(dev_id, {}).get("name") or name)
            for sid, sreg in ents.items():
                sst = by_id.get(sid, {})
                if (dev_id and sreg.get("device_id") == dev_id and sid.startswith("sensor.")
                        and not sreg.get("disabled_by")
                        and sst.get("attributes", {}).get("device_class") in READING_CLASSES):
                    label = sst["attributes"].get("friendly_name", sid)
                    if label.lower().startswith(dev_name.lower()):
                        label = label[len(dev_name):].strip(" -_")
                    readings.setdefault(eid, []).append((sid, label or "reading"))
        if len(out) > max_entities:
            log.warning("%d controllable entities; keeping %d to fit the prompt - "
                        "expose fewer in HA (Settings > Voice assistants > Expose)",
                        len(out), max_entities)
            out = dict(sorted(out.items())[:max_entities])
        self.readings = {k: v for k, v in readings.items() if k in out}
        log.info("%d entities from HA (%s)", len(out),
                 "exposed to Assist" if exposed else f"all in {sorted(domains)}")
        return out
