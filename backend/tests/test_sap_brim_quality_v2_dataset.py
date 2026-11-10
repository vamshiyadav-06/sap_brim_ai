"""Guard the source references and coverage categories in the reviewed quality set."""

import json
from pathlib import Path


DATASET = Path(__file__).resolve().parents[1] / "evaluation" / "datasets" / "sap_brim_quality_v2.jsonl"
REQUIRED_CATEGORIES = {
    "basic_conceptual",
    "detailed_technical",
    "process_flow",
    "scenario_based",
    "database_table",
    "transaction_code",
    "integration",
    "follow-up",
    "insufficient_evidence",
}


def test_curated_quality_rows_have_reviewable_evidence_and_requirements():
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) >= 10
    assert REQUIRED_CATEGORIES <= {row["category"] for row in rows}
    assert len({row["id"] for row in rows}) == len(rows)

    documents_dir = Path(__file__).resolve().parents[2] / "knowledge_base" / "documents"
    for row in rows:
        assert row["query"].strip()
        assert row["review_status"] == "source_checked_not_sme_validated"
        assert row["required_claims"]
        if row["answerable"]:
            assert row["expected_evidence"]
        else:
            assert not row["expected_evidence"]
            assert row["evidence_gaps"]
        for evidence in row["expected_evidence"]:
            assert (documents_dir / evidence["document"]).is_file()
            assert isinstance(evidence["page"], int) and evidence["page"] > 0

