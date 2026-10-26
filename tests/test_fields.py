from datetime import date
from pathlib import Path

from cmrmp.extract import fields

AVIS = (Path(__file__).parent / "fixtures" / "avis_synthetique.txt").read_text(encoding="utf-8")


def test_parse_fcfa():
    assert fields.parse_fcfa("3 100 500") == 3100500
    assert fields.parse_fcfa("22.800.000") == 22800000
    assert fields.parse_fcfa("1,5 million") == 1500000


def test_avis_fields():
    f = fields.avis_fields(AVIS)
    assert f["reference"] == "025/AONO/CAYI/2026"
    assert f["procedure"] == "AONO"
    assert f["cout_dossier_fcfa"] == 100000
    assert f["caution_fcfa"] == 3100500
    assert f["capacite_financiere_fcfa"] == 50000000
    assert f["cout_previsionnel_fcfa"] == 103350000
    assert f["date_depot"] == date(2026, 4, 22)
    assert f["date_ouverture"] == date(2026, 4, 22)


def test_identifiers():
    text = "NIU : M011500012345A, RCCM RC/YAO/2015/B/1234, tél. +237 699 12 34 56, contact@ets-alpha.cm"
    assert fields.niu_list(text) == ["M011500012345A"]
    assert fields.rccm_list(text) == ["RC/YAO/2015/B/1234"]
    assert fields.phone_list(text) == ["699123456"]
    assert fields.email_list(text) == ["contact@ets-alpha.cm"]


def test_classify_and_labels():
    assert fields.classify("Décision d'attribution N°012") == "decision_attribution"
    assert fields.classify("PROCES-VERBAL D'OUVERTURE DES PLIS") == "pv_ouverture"
    assert fields.classify("ADDITIF N°01") == "additif"
    lv = fields.label_values("ATTRIBUTAIRE : STE AM ET CIE SARL\nMONTANT : 22.800.000 F.CFA\nDELAI : 04 MOIS")
    assert lv["attributaire"] == "STE AM ET CIE SARL"
    assert fields.parse_fcfa(lv["montant"]) == 22800000
