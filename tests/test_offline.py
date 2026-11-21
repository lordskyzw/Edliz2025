"""Offline tests: parsing and fusion logic, no network, no database.

    .venv/bin/python -m pytest tests -q
"""

from __future__ import annotations

import numpy as np

from app import medicines, textproc
from app.retrieval import Hit
from app.tools import format_results


# --------------------------------------------------------------------------- #
# OCR normalisation
# --------------------------------------------------------------------------- #

def test_route_artifacts_are_repaired():
    assert "iv/sc" in textproc.normalise("Adrenaline ivisc 0.01mg/kg")
    assert "iv/im" in textproc.normalise("Benzylpenicillin ivlim 0.1MU")


def test_doses_are_never_altered():
    raw = "ceftriaxone im 250mg one dose only 7 days"
    assert textproc.normalise(raw) == raw


def test_table_rules_are_stripped():
    assert "___" not in textproc.normalise("miconazole po C ___V___ 200mg")


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #

def test_chunks_respect_size_and_keep_page():
    text = "\n\n".join(f"Sentence number {i} about treatment." for i in range(200))
    chunks = textproc.chunk_page(42, text, target=400, overlap=50)
    assert chunks
    assert all(c.page == 42 for c in chunks)
    assert max(len(c.text) for c in chunks) < 800


def test_short_page_produces_nothing():
    assert textproc.chunk_page(1, "   ") == []


# --------------------------------------------------------------------------- #
# Medicines index
# --------------------------------------------------------------------------- #

def test_treatment_table_row():
    name, form = medicines.parse_line("cotrimoxazole po C V_ 1920mg 3 times a day 21 days")
    assert name == "cotrimoxazole"
    assert form and "1920mg" in form


def test_dosage_table_row():
    name, form = medicines.parse_line("Amoxicillin 30mg/kg/dose 2 to 4")
    assert name == "Amoxicillin"
    assert form and form.startswith("30mg")


def test_row_without_a_dose_is_still_accepted():
    name, _ = medicines.parse_line("artemether-lumefantrine po C V_ see table")
    assert name == "artemether-lumefantrine"


def test_prose_is_rejected():
    assert medicines.parse_line("Antiviral treatment should be started as soon as possible") is None
    assert medicines.parse_line("but not less than 150 mg") is None
    assert medicines.parse_line("lower tidal volumes 8 ml") is None
    assert medicines.parse_line("Children: pyridoxine 25mg daily") is None


def test_name_key_normalises():
    assert medicines.name_key("Benzylpenicillin (3g)") == "benzylpenicillin"
    assert medicines.name_key("Amoxicillin/Clavulanate") == "amoxicillin clavulanate"


# --------------------------------------------------------------------------- #
# Retrieval fusion
# --------------------------------------------------------------------------- #

def test_agreement_beats_a_single_signal():
    """A chunk both searches like must outrank one only one of them likes."""
    both = Hit(1, 1, None, "")
    both.vector_rank, both.keyword_rank = 1, 1
    vector_only = Hit(2, 2, None, "")
    vector_only.vector_rank = 1

    def rrf(hit: Hit) -> float:
        score = 0.0
        if hit.vector_rank:
            score += 1.0 / (60 + hit.vector_rank)
        if hit.keyword_rank:
            score += 1.0 / (60 + hit.keyword_rank)
        if hit.vector_rank and hit.keyword_rank:
            score *= 1.15
        return score

    assert rrf(both) > rrf(vector_only)


# --------------------------------------------------------------------------- #
# Web result numbering
# --------------------------------------------------------------------------- #

def test_web_numbering_continues_across_calls():
    first = [{"title": "A", "url": "u1", "snippet": "", "source": "web"}]
    second = [{"title": "B", "url": "u2", "snippet": "", "source": "pubmed"}]
    a = format_results(first, start=1)
    b = format_results(second, start=2)
    assert a.startswith("[W1]")
    assert b.startswith("[W2] (PubMed)")


def test_empty_results_say_unavailable():
    assert "unavailable" in format_results([]).lower()


# --------------------------------------------------------------------------- #
# Vectors
# --------------------------------------------------------------------------- #

def test_embeddings_are_unit_length():
    from app import caimex

    vecs = caimex.embed(["ceftriaxone gonorrhoea"])
    assert vecs.shape[1] > 0
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-4)
