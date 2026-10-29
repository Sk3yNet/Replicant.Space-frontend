from rsweb.web import filter_devices, tag_matches, tag_options


DEVS = [{"device_code": "A", "tags": ["at:sol-3", "spare"]}, {"device_code": "B", "tags": ["at:delta-2", "fleet:miner-1"]},
        {"device_code": "C", "tags": ["to:sol", "contract:x"]}, {"device_code": "D", "tags": []}]


def test_tag_kinds_fold_into_one_entry():
    o = tag_options(DEVS)
    assert o["plain"] == [("spare", 1)] and o["fleets"] == [("fleet:miner-1", 1)]
    assert [g[0] for g in o["groups"]] == ["at:", "to:", "contract:"]
    assert o["groups"][0][2:] == (2, 2)          # two pins, two devices
    assert tag_options(DEVS, "at:sol-3")["extra"] == "at:sol-3"     # a single pin from a link stays selectable
    assert tag_options(DEVS, "at:")["extra"] == "" and tag_options(DEVS, "spare")["extra"] == ""


def test_kind_filter_matches_every_tag_of_that_kind():
    assert tag_matches("at:", ["at:sol-3"]) and not tag_matches("at:", ["spare"])
    assert tag_matches("at:sol-3", ["at:sol-3"]) and not tag_matches("at:sol", ["at:sol-3"])
    assert [d["device_code"] for d in filter_devices(DEVS, "", "", "", "", "at:")] == ["A", "B"]
