from rsweb.web_devices import away_owner_note


def test_away_owner_note_names_owner_and_replicants_here():
    devs = [
        {"device_code": "410EFEBA", "device_type": "crystal_seed", "location": "DELTA-OBJ-2", "replicant_code": "7F509D43"},
        {"device_code": "BE6E8367", "location": "FALQUORYX-1-L4", "replicant_code": "7F509D43",
         "hosting_replicant": {"name": "Augustin", "replicant_code": "7F509D43"}},
        {"device_code": "BFE1BB3A", "location": "DELTA-OBJ-2", "replicant_code": "0BC10C52",
         "hosting_replicant": {"name": "Rufus", "replicant_code": "0BC10C52"}},
    ]
    n = away_owner_note("410EFEBA", devs)
    assert "belongs to Augustin, who is at FALQUORYX-1-L4" in n
    assert "Hand it to Rufus (0BC10C52) with change_owner" in n
    n = away_owner_note("410EFEBA", devs[:2])
    assert "Bring the owner here" in n
    assert away_owner_note("NOPE", devs) is None
