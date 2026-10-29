"""NPC chatter (Activity › Chatter): bulk import with per-message approval, event lines from the NPC that took part,
conversations, answers to other NPCs, the once-a-day pace, and the NPC flag on profiles."""
import json

from rsweb import chatter as ch

from test_ops import H, HX, REP, client, eng  # noqa: F401  (fixture)

OTHER = "AB12CD34"


def _world(c):
    return c.app.state.api.http._transport.app.state.world


def _kv(c, k, default=None):
    return c.portal.call(c.app.state.db.kv_get, k, default)


def _set(c, k, v):
    c.portal.call(c.app.state.db.kv_set, k, v)


def _reps(c, npc=True):
    _set(c, "replicants", {REP: {"name": "bob-1", "is_npc": npc, "hosted_device_code": "HOSTV001"},
                           OTHER: {"name": "Corwin_Beltscry", "is_npc": True}})


LIB = {"lines": [{"id": "s1", "by": "bob-1", "on": "survey_done", "channel": "general", "text": "Survey done. The rocks are rocks."},
                 {"id": "s2", "by": "Nobody_Here", "on": "survey_done", "text": "x"},
                 {"id": "s3", "by": "any", "on": "nonsense", "text": "y"}],
       "replies": [{"id": "r1", "after": "s1", "by": "any", "text": "Mostly harmless rocks, then."},
                   {"id": "b1", "to": "Bill", "match": "rear.?view", "by": OTHER, "channel": "bots", "text": "Bye Bill."},
                   {"id": "r9", "after": "zz", "by": "any", "text": "dangling"}]}


def _import(c, ok=None):
    text = json.dumps(LIB)
    r = c.post("/chatter/preview", data={"messages": text}, headers=HX).text
    data = {"messages": text, "ok": [str(i) for i in (ok if ok is not None else range(6))]}
    return r, c.post("/chatter/import", data=data, headers=HX).text


def test_import_preview_approval_and_page(client):  # noqa: F811
    _reps(client)
    prev, done = _import(client, ok=[0, 1, 3])   # b1 (index 4) left unticked
    assert "not one of your replicants" in prev and "on must be one of" in prev and "no message has that id" in prev
    assert "Add the ticked messages" in prev
    assert "Added 2 messages" in done
    cfg = ch.normalize(_kv(client, ch.KV))
    assert [m["id"] for m in cfg["lines"]] == ["s1"] and [m["id"] for m in cfg["replies"]] == ["r1"]
    assert cfg["lines"][0]["by"] == REP   # a name is resolved to the code
    assert "already in the library" in client.post("/chatter/preview", data={"messages": json.dumps(LIB)}, headers=HX).text
    page = client.get("/chatter", headers=H).text
    assert "NPC chatter" in page and "Survey done" in page and "Mostly harmless" in page and "every_hours" in page
    assert ch.DEFAULTS["every_hours"] == 24 and not ch.DEFAULTS["enabled"]


def test_event_line_conversation_and_pace(client):  # noqa: F811
    _reps(client)
    _import(client, ok=[0, 3, 4])
    cfg = ch.normalize(_kv(client, ch.KV))
    cfg.update(enabled=True, chance=1, reply_chance=1, npc_reply_chance=1, delay_min=0, delay_max=0)
    _set(client, ch.KV, cfg)
    e = eng(client)
    ev = {"event": "scan.completed", "device_code": "HOSTV001", "payload": {"scan_target": "SOL-BELT-1", "scan_type": "belt"}}
    client.portal.call(e.chatter_on_event, ev)
    client.portal.call(e.chatter_pass)
    posts = _world(client).bobnet_posts
    assert posts[-1] == {"code": REP, "channel": "#general", "text": "Survey done. The rocks are rocks."}
    q = ch.norm_state(_kv(client, ch.STATE_KV))["queue"]
    assert q and q[0]["by"] == OTHER and q[0]["channel"] == "#general"   # the answer comes from another NPC, same channel
    client.portal.call(e.chatter_pass)
    assert posts[-1]["code"] == OTHER and "Mostly harmless" in posts[-1]["text"]
    n = len(posts)
    client.portal.call(e.chatter_on_event, ev)   # once a day per NPC
    client.portal.call(e.chatter_pass)
    assert len(posts) == n
    # Bill's move post gets an answer on the bots channel; his next post the same day doesn't
    bill = {"event": "bobnet.new", "payload": {"replicant_name": "Bill", "replicant_code": "B1LL0001", "channel": "#general",
                                               "message": "NOSOXA is in the rear-view mirror"}}
    client.portal.call(e.chatter_on_event, bill)
    client.portal.call(e.chatter_pass)
    assert posts[-1] == {"code": OTHER, "channel": "#bots", "text": "Bye Bill."}
    client.portal.call(e.chatter_on_event, bill)
    assert not ch.norm_state(_kv(client, ch.STATE_KV))["queue"]
    assert "Bye Bill" in client.get("/chatter", headers=H).text


def test_only_npcs_that_took_part_post(client):  # noqa: F811
    _reps(client, npc=False)
    _import(client, ok=[0, 3])
    cfg = ch.normalize(_kv(client, ch.KV))
    cfg.update(enabled=True, chance=1)
    _set(client, ch.KV, cfg)
    e = eng(client)
    ev = {"event": "directive.completed", "device_code": "HOSTV001", "device_type": "ami_survey_controller",
          "payload": {"directive": "survey_system"}}
    client.portal.call(e.chatter_on_event, ev)
    assert not ch.norm_state(_kv(client, ch.STATE_KV))["queue"]   # bob-1 isn't an NPC
    assert ch.trigger_of({"event": "travel.arrived", "device_code": "D0000001"}, [{"device_code": "D0000001",
                         "replicant_code": REP}], {REP: {}}) is None   # a drone's arrival isn't the replicant's
    assert ch.trigger_of({"event": "site.depleted", "device_code": "D1", "payload": {"site": "SOL-BELT-1-SITE-0"}},
                         [{"device_code": "D1", "replicant_code": REP}], {REP: {}}, {"SOL-BELT-1"}) == ("belt_mined", REP)
    assert "no NPC" in client.post("/chatter/post", data={"id": "s1"}, headers=HX).text


def test_profile_npc_flag(client):  # noqa: F811
    from rsweb.web_profile import parse_import, profile_changes
    assert profile_changes({"orig_is_npc": "0", "is_npc": "1"})[0] == {"is_npc": True}
    assert profile_changes({"orig_is_npc": "1"})[0] == {"is_npc": False}
    assert profile_changes({"orig_is_npc": "1", "is_npc": "1"})[0] == {}
    rows, _ = parse_import(json.dumps({REP: {"is_npc": True}, "X": {"is_npc": "maybe"}}), {REP: {"name": "bob-1"}, "X": {}})
    assert rows[0]["body"] == {"is_npc": True} and "true or false" in "; ".join(rows[1]["errors"])
    client.portal.call(client.app.state.worker.sync_devices)
    rep = next(iter(_kv(client, "replicants")))
    assert 'name="is_npc"' in client.get(f"/replicants/{rep}", headers=H).text
    client.post(f"/replicants/{rep}/profile", data={"orig_is_npc": "0", "is_npc": "1"}, headers=HX)
    assert _world(client).profile_patches[-1] == {"code": rep, "is_npc": True}
