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
    _set(c, "replicants", {REP: {"name": "bob-1", "is_npc": npc, "hosted_device_code": "HOSTV001", "current_star": "SOL"},
                           OTHER: {"name": "Corwin_Beltscry", "is_npc": True, "current_star": "SOL"}})


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
    assert "NPC chatter" in page and "Survey done" in page and "Mostly harmless" in page and "lim_general" in page
    assert ch.normalize({})["limits"] == {"general": 24, "bots": 24} and not ch.DEFAULTS["enabled"]
    assert ch.normalize({"every_hours": 12, "bots_hours": 2})["limits"] == {"general": 12, "bots": 2}   # older settings


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


def test_placeholders_from_bills_post_and_no_quick_repeats(client):  # noqa: F811
    _reps(client)
    _set(client, "stars", {"stars": [{"designation": "NOSOXA", "region": "outer"}, {"designation": "SOL", "region": "solzone"}]})
    lib = {"replies": [{"id": "bf1", "to": "Bill", "by": OTHER, "channel": "general", "text": "The girls of {from} are sad to see you go, Bill."},
                       {"id": "bf2", "to": "Bill", "by": OTHER, "text": "Nice {nope}."}],
           "lines": [{"id": "l1", "by": "any", "on": "arrived", "text": "Hello {star} in {region}. Hi {poster}."}]}
    r = client.post("/chatter/preview", data={"messages": json.dumps(lib)}, headers=HX).text
    assert "unknown placeholder {nope}" in r and "only filled in replies" in r
    cfg = ch.normalize({"enabled": True, "npc_reply_chance": 1, "delay_min": 0, "delay_max": 0,
                        "replies": [lib["replies"][0], {"id": "bf3", "to": "Bill", "by": OTHER, "text": "Bye from {from}."}]})
    _set(client, ch.KV, cfg)
    e = eng(client)
    plain = {"event": "bobnet.new", "payload": {"replicant_name": "Bill", "channel": "#general", "current_star": "SOL",
                                                "message": "Hello everyone"}}
    client.portal.call(e.chatter_on_event, plain)   # names no system: no answer that needs {from}
    assert not ch.norm_state(_kv(client, ch.STATE_KV))["queue"]
    moved = {"event": "bobnet.new", "payload": {"replicant_name": "Bill", "channel": "#general", "current_star": "SOL",
                                                "message": "Skunkworks is on the move. NOSOXA is in the rear-view mirror."}}
    client.portal.call(e.chatter_on_event, moved)
    client.portal.call(e.chatter_pass)
    assert _world(client).bobnet_posts[-1]["text"] in ("The girls of NOSOXA are sad to see you go, Bill.", "Bye from NOSOXA.")
    st = {"posted": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
    opts = [{"id": x} for x in "abcd"]
    assert ch.pick(opts, ch.norm_state(st))["id"] == "d"   # never posted first
    assert ch.pick(opts[:3], ch.norm_state(st))["id"] == "a"   # then the longest ago
    assert ch.post_values({"current_star": "SOL", "message": "SOL and NOSOXA"}, {"SOL": {}, "NOSOXA": {"region": "outer"}}) \
        == {"poster": None, "star": "SOL", "from": "NOSOXA", "region": "outer", "arrival": None, "pilot": "SOL", "score": None}


def test_write_a_message_by_hand(client):  # noqa: F811
    _reps(client, npc=False)
    client.portal.call(client.app.state.db.insert_event, {"id": "b1", "event": "bobnet.new",
                                                          "payload": {"channel": "#lounge", "message": "hi"}})
    page = client.get("/chatter", headers=H).text
    assert "#lounge" in page
    assert "Write a message" in page and "#bots" in page and "#trade" in page
    r = client.post("/chatter/write", data={"code": REP, "channel": "#general", "other": "sk3y-bots", "text": "Hello"}, headers=HX)
    assert r.headers.get("X-Sent") == "1" and _world(client).bobnet_posts[-1] == {"code": REP, "channel": "#sk3y-bots", "text": "Hello"}
    assert ch.norm_state(_kv(client, ch.STATE_KV))["posted"][-1]["why"] == "written by hand"
    assert "Not one of your" in client.post("/chatter/write", data={"code": "NOPE", "channel": "#general", "text": "x"}, headers=HX).text


def test_mentions_match_each_part_of_an_underscored_name():
    from rsweb.notify import mentions
    names = ["Hester_Towline", "Sk3y"]
    assert mentions("hey towline!", names) and mentions("HESTER?", names) and mentions("Hester_Towline rocks", names)
    assert not mentions("Sk3yNet", names) and not mentions("Towlines", names) and mentions("@Sk3y", names)
    assert not mentions("Pip went home", ["Pi_Xy"])   # parts under 3 letters are left out


def test_arrival_placeholder_from_rikers_post():
    v = ch.post_values({"replicant_name": "Riker", "current_star": "SOL", "message": "Heads up folks - Jim-1 just arrived back "
                        "in SOL from deep space. Another replicant returned home. Welcome them if you see them."}, {"SOL": {}})
    assert v["arrival"] == "Jim-1" and v["from"] is None
    cfg = ch.normalize({"replies": [{"id": "a", "to": "Riker", "match": "just arrived", "text": "Welcome back, {arrival}."},
                                    {"id": "b", "to": "Riker", "text": "Hi Riker."}]})
    opts = ch.fillable(ch.npc_replies("Riker", "Jim-1 just arrived back in SOL", cfg), v)
    assert [o["id"] for o in opts] == ["a"] and ch.fill(opts[0]["text"], v) == "Welcome back, Jim-1."


def test_timer_per_channel_and_another_npc_tries_a_refused_reply(client, monkeypatch):  # noqa: F811
    _reps(client)
    third = "EF56AB78"
    reps = _kv(client, "replicants")
    reps[third] = {"name": "Pip_Parcelwick", "is_npc": True, "current_star": "LERNA"}
    _set(client, "replicants", reps)
    cfg = ch.normalize({"enabled": True, "chance": 1, "npc_reply_chance": 1, "reply_chance": 0, "delay_min": 0, "delay_max": 0,
                        "limits": {"general": 24, "bots": 0.5, "#lounge": 24},
                        "lines": [{"id": "g", "by": REP, "on": "survey_done", "channel": "general", "text": "General line."},
                                  {"id": "b", "by": REP, "on": "survey_done", "channel": "bots", "text": "Bots line."}],
                        "replies": [{"id": "r", "to": "Riker", "by": "any", "channel": "#lounge", "text": "Hi Riker."}]})
    _set(client, ch.KV, cfg)
    assert ch.hours_for(cfg, "#bots") == 0.5 and ch.hours_for(cfg, "#general") == 24 and ch.hours_for(cfg, "#trade") is None
    e = eng(client)
    ev = {"event": "scan.completed", "device_code": "HOSTV001", "payload": {"scan_target": "SOL-BELT-1"}}
    posts = _world(client).bobnet_posts
    for _ in range(3):
        client.portal.call(e.chatter_on_event, ev)
        client.portal.call(e.chatter_pass)
    assert sorted(p["text"] for p in posts) == ["Bots line.", "General line."]   # one per channel, then both wait
    _world(client).bobnet_refuse.update({REP, OTHER, third})
    _world(client).bobnet_refuse.discard(third)
    monkeypatch.setattr(ch, "speaker_for", lambda m, other, reps, rnd=None, can=None: REP)   # the first try is refused
    riker = {"event": "bobnet.new", "payload": {"replicant_name": "Riker", "channel": "#general", "message": "Hello"}}
    for _ in range(4):
        client.portal.call(e.chatter_on_event, riker)
        client.portal.call(e.chatter_pass)
    assert posts[-1] == {"code": third, "channel": "#lounge", "text": "Hi Riker."}
    fails = [p for p in ch.norm_state(_kv(client, ch.STATE_KV))["posted"] if not p["ok"]]
    assert fails and fails[0]["by"] == REP and all(p.get("retry") for p in fails)
    assert len([p for p in posts if p["text"] == "Hi Riker."]) == 1   # one NPC post a day on #lounge


def test_no_active_relay_systems_are_skipped():
    reps = {"A": {"is_npc": True, "current_star": "SOL"}, "B": {"is_npc": True, "current_star": "LERNA"},
            "C": {"is_npc": True, "current_star": "FAR"}}
    relays = ch.relay_stars([{"device_type": "ftl_relay", "location": "SOL-3-L4", "status": "active"},
                             {"device_type": "ftl_relay", "location": "FAR-3-L4", "status": "inactive"}])
    assert relays == {"SOL"}
    state = ch.norm_state({"no_relay": {"FAR": ch.now_iso()}})
    can = ch.poster_check(reps, relays, state)
    assert can("A") == (True, True) and can("B") == (True, False) and can("C")[0] is False
    assert ch.speaker_for({"by": "any"}, None, reps, can=can) == "A"   # our relay first
    assert ch.speaker_for({"by": "C"}, None, reps, can=can) is None


def test_channel_limiters_in_settings(client):  # noqa: F811
    r = client.post("/chatter/settings", data={"lim_general": "12", "lim_bots": "", "lim_ch": ["trade", "#x", ""],
                                               "lim_h": ["6", "", "3"]}, headers=HX)
    assert "Saved" in r.text
    cfg = ch.normalize(_kv(client, ch.KV))
    assert cfg["limits"] == {"general": 12.0, "bots": None, "#trade": 6.0}
    assert ch.hours_for(cfg, "#bots") is None and not ch.ready(ch.norm_state({}), "#bots", cfg)
    assert ch.ready(ch.norm_state({}), "#trade", cfg)
    assert "#trade" in client.get("/chatter", headers=H).text


def test_pilot_and_score_placeholders_from_patch():
    v = ch.post_values({"message": "creme-1707 exits the field in decent shape. Respectable flying. Score: 419"}, {})
    assert v["pilot"] == "creme-1707" and v["score"] == "419"
    assert ch.post_values({"message": "I'm picking up creme-1707 on sensors"}, {})["pilot"] == "creme-1707"
    assert ch.post_values({"message": "Quiet start from creme-1707 - barely a shimmer"}, {})["pilot"] == "creme-1707"
    assert ch.post_values({"message": "The Drafel are not impressed."}, {})["pilot"] is None
    assert ch.post_values({"message": "limps out. Score: 0"}, {})["score"] == "0"
