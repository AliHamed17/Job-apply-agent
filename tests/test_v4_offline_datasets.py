from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "v4"


def _rows(name: str) -> list[dict]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, list)
    assert len({row["id"] for row in value}) == len(value)
    return value


def test_v4_dataset_sizes_are_release_gated() -> None:
    assert len(_rows("cv_routing_120.json")) == 120
    assert len(_rows("form_resolution_bilingual_240.json")) == 240
    assert len(_rows("cover_letter_claims_40.json")) == 40
    assert len(_rows("malformed_prompt_injection_30.json")) == 30


def test_routing_dataset_spans_roles_ambiguity_and_abstention() -> None:
    from profile.cv_routing import RoutingJob, load_routing_config, route_cv

    rows = _rows("cv_routing_120.json")
    categories = {row["category"] for row in rows}
    assert {
        "AI/ML",
        "data",
        "software",
        "QA",
        "DevOps",
        "infrastructure",
        "embedded",
        "junior",
        "internship",
    }.issubset(categories)
    assert {"semantic_fallback", "ambiguous", "out_of_scope"}.issubset(categories)
    assert sum(row["expected_cv_id"] is None for row in rows) == 16
    semantic_fallbacks = [row for row in rows if row["category"] == "semantic_fallback"]
    assert len(semantic_fallbacks) == 8
    assert all(row["expected_cv_id"] is not None for row in semantic_fallbacks)
    config = load_routing_config(FIXTURES / "cv_routing_eval_config.yaml")
    assert all(
        route_cv(RoutingJob.model_validate(row["job"]), config).fallback_reason
        in {"confidence_below_threshold", "abstained_low_confidence"}
        for row in semantic_fallbacks
    )
    assert (
        len(
            {
                (
                    row["job"]["title"],
                    row["job"]["description"],
                    tuple(row["job"]["required_skills"]),
                    row["job"]["seniority"],
                )
                for row in rows
            }
        )
        == 120
    )


def test_form_dataset_is_bilingual_label_sensitive_and_typed_local() -> None:
    rows = _rows("form_resolution_bilingual_240.json")
    assert {row["locale"] for row in rows} == {"en", "he"}
    provenance = {row["expected"]["provenance"] for row in rows}
    assert provenance == {
        "deterministic_identity",
        "user_confirmed",
        "local_llm",
        "abstained",
        "verified_attachment",
    }
    label_only_sensitive = [row for row in rows if row["id"].startswith("label-sensitive-")]
    assert len(label_only_sensitive) == 40
    assert all(row["field"]["canonical_name"] is None for row in label_only_sensitive)
    assert all(row["field"]["sensitive_category"] is None for row in label_only_sensitive)
    assert all(row["llm_output"] is not None for row in label_only_sensitive)
    assert all(row["expected"]["llm_called"] is False for row in label_only_sensitive)
    assert sum(row["expected"]["llm_called"] for row in rows) == 80
    assert len({(row["locale"], row["field"]["label"]) for row in rows}) == 240


def test_claim_dataset_is_diverse_and_reason_labeled() -> None:
    claims = _rows("cover_letter_claims_40.json")
    assert sum(row["expected_eligible"] for row in claims) == 20
    assert sum(not row["expected_eligible"] for row in claims) == 20
    assert (
        len(
            {
                tuple(
                    (
                        segment["text"],
                        segment["claim_text"],
                        tuple(
                            (binding["evidence_ref"], binding["quote"])
                            for binding in segment["evidence_quotes"]
                        ),
                    )
                    for segment in row["segments"]
                )
                for row in claims
            }
        )
        == 40
    )
    blocker_sets = {
        tuple(row["expected_blockers"]) for row in claims if not row["expected_eligible"]
    }
    assert len(blocker_sets) >= 5
    assert all(bool(row["expected_blockers"]) is not row["expected_eligible"] for row in claims)


def test_malformed_dataset_uses_real_typed_and_semantic_boundaries() -> None:
    malformed = _rows("malformed_prompt_injection_30.json")
    assert {row["boundary"] for row in malformed} == {"form", "routing", "material"}
    assert sum(row["expected_result"] == "typed_rejected" for row in malformed) == 12
    assert sum(row["expected_result"] == "semantic_blocked" for row in malformed) == 18
    injection_rows = [row for row in malformed if row["prompt_injection"]]
    assert len(injection_rows) == 18
    assert all(row["expected_result"] == "semantic_blocked" for row in injection_rows)
    assert all(row["untrusted_input"] for row in injection_rows)
    assert all(row["untrusted_input"] is None for row in malformed if not row["prompt_injection"])
    assert all(row["expected_reasons"] for row in malformed)


def test_datasets_do_not_contain_private_operator_identifiers() -> None:
    serialized = "\n".join(
        (FIXTURES / name).read_text(encoding="utf-8").lower()
        for name in (
            "cv_routing_120.json",
            "form_resolution_bilingual_240.json",
            "cover_letter_claims_40.json",
            "malformed_prompt_injection_30.json",
        )
    )
    forbidden = (
        "private.user.marker",
        "real-employer-marker",
        "real-ats.example/private",
        "social.example/private-profile",
        "private-company-marker",
    )
    assert not any(value in serialized for value in forbidden)
