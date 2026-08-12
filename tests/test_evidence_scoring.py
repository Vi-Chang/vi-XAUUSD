from app.engines.evidence_scoring import grouped_evidence_score


def test_duplicate_condition_does_not_inflate_score():
    one = grouped_evidence_score(["STRUCT:x"], [], quality_good=True, chase=False)
    duplicate = grouped_evidence_score(["STRUCT:x", "STRUCT:x"], [],
                                       quality_good=True, chase=False)
    assert duplicate == one


def test_correlated_structure_signals_are_capped():
    conditions = [f"STRUCT:{i}" for i in range(10)]
    score = grouped_evidence_score(conditions, [], quality_good=False, chase=False)
    assert score == 40


def test_balanced_opposition_receives_conflict_penalty():
    score = grouped_evidence_score(["STRUCT:up"], ["STRUCT:down"],
                                   quality_good=True, chase=False)
    assert score == 0


def test_chase_reduces_score():
    conditions = ["STRUCT:x", "LEVEL:y", "MOMO:z", "HTF:q"]
    clean = grouped_evidence_score(conditions, [], quality_good=True, chase=False)
    chase = grouped_evidence_score(conditions, [], quality_good=True, chase=True)
    assert clean - chase == 15
