import pandas as pd

from cmrmp.build import entities


def test_normalize_and_key():
    assert entities.normalize_name("Ets. Alpha & Fils S.A.R.L") == "ETS ALPHA ET FILS SARL"
    assert entities.name_key("ETS ALPHA ET FILS SARL") == "ALPHA ET FILS"


def test_resolve_merges_variants_and_ids():
    m = pd.DataFrame({
        "nom_brut": ["ETS ALPHA ET FILS SARL", "Alpha & Fils", "BETA BTP SA", "BETA B.T.P", "GAMMA SERVICES"],
        "niu": [None, None, "M011500012345A", "M011500012345A", None],
    })
    out, pairs = entities.resolve(m)
    ids = out.set_index("nom_brut")["entity_id"]
    assert ids["ETS ALPHA ET FILS SARL"] == ids["Alpha & Fils"]
    assert ids["BETA BTP SA"] == ids["BETA B.T.P"]
    assert ids["GAMMA SERVICES"] != ids["BETA BTP SA"]


def test_groupement():
    assert entities.is_groupement("GROUPEMENT ALPHA SARL / BETA SA")
    assert entities.split_groupement("GROUPEMENT ALPHA SARL / BETA SA") == ["ALPHA SARL", "BETA SA"]
