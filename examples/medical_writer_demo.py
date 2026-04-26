"""Smoke test demo — seeds a 3-page manuscript with citations + MCQs.

Run with:
    python -m selran_canvas --demo

Then open the printed URL. You should see:
    - Three pages in the sidebar (Introduction, Methods, Results)
    - In-text citations rendered in Vancouver style by default
    - A bibliography panel bottom-right
    - Two pending MCQ cards (one on Methods, one on Results)
    - A reference list with 4 entries

Switch the journal dropdown in the topbar to e.g. "the-new-england-journal-of-medicine"
and watch every citation re-format instantly. Toggle the Mode dropdown to "Manuscript"
to see all three pages stitched into a single submission view with page numbers.
"""
from __future__ import annotations

from selran_canvas.store import Store


def seed_demo(store: Store) -> None:
    # ---- References (CSL-JSON) -----------------------------------------
    references = [
        {
            "id": "polack2020",
            "type": "article-journal",
            "author": [{"family": "Polack", "given": "FP"}, {"family": "Thomas", "given": "SJ"}, {"family": "Kitchin", "given": "N"}],
            "title": "Safety and Efficacy of the BNT162b2 mRNA COVID-19 Vaccine",
            "container-title": "New England Journal of Medicine",
            "container-title-short": "N Engl J Med",
            "volume": "383", "issue": "27", "page": "2603-2615",
            "issued": {"date-parts": [[2020, 12, 31]]},
            "DOI": "10.1056/NEJMoa2034577", "PMID": "33301246",
        },
        {
            "id": "thaweethai2023",
            "type": "article-journal",
            "author": [{"family": "Thaweethai", "given": "T"}, {"family": "Jolley", "given": "SE"}],
            "title": "Development of a Definition of Postacute Sequelae of SARS-CoV-2 Infection",
            "container-title": "JAMA",
            "container-title-short": "JAMA",
            "volume": "329", "issue": "22", "page": "1934-1946",
            "issued": {"date-parts": [[2023, 6, 13]]},
            "DOI": "10.1001/jama.2023.8823",
        },
        {
            "id": "soriano2022",
            "type": "article-journal",
            "author": [{"family": "Soriano", "given": "JB"}, {"family": "Murthy", "given": "S"}, {"family": "Marshall", "given": "JC"}],
            "title": "A clinical case definition of post-COVID-19 condition by a Delphi consensus",
            "container-title": "The Lancet Infectious Diseases",
            "container-title-short": "Lancet Infect Dis",
            "volume": "22", "issue": "4", "page": "e102-e107",
            "issued": {"date-parts": [[2022, 4, 1]]},
            "DOI": "10.1016/S1473-3099(21)00703-9",
        },
        {
            "id": "vandyck2023",
            "type": "article-journal",
            "author": [{"family": "van Dyck", "given": "CH"}, {"family": "Swanson", "given": "CJ"}, {"family": "Aisen", "given": "P"}],
            "title": "Lecanemab in Early Alzheimer's Disease",
            "container-title": "New England Journal of Medicine",
            "container-title-short": "N Engl J Med",
            "volume": "388", "issue": "1", "page": "9-21",
            "issued": {"date-parts": [[2023, 1, 5]]},
            "DOI": "10.1056/NEJMoa2212948",
        },
    ]
    store.upsert_references(references)

    # ---- Pages ---------------------------------------------------------
    store.upsert_page(
        "introduction",
        "Introduction",
        """COVID-19 vaccines have transformed pandemic management. The BNT162b2 mRNA vaccine demonstrated 95% efficacy in adults [@polack2020], a result that established the foundation for population-level deployment programmes worldwide. The post-acute sequelae of SARS-CoV-2 infection (PASC) — colloquially "Long COVID" — is now defined per WHO criteria as symptoms persisting ≥3 months after acute infection without alternate explanation [@soriano2022], and the NIH RECOVER programme has operationalised the definition into a 12-symptom score [@thaweethai2023].

This protocol proposes a Phase IV pragmatic trial to evaluate whether early antiviral exposure modifies the trajectory of PASC symptom resolution.

<!--mcq:framing-->

The trial leverages decentralised methodology and patient-reported outcomes captured via ePRO, with a target sample of 2,400 participants randomised 1:1 to early antiviral treatment versus standard care.""",
    )

    store.upsert_page(
        "methods",
        "Methods",
        """## Study Design

A pragmatic, decentralised, parallel-group, open-label randomised controlled trial. Participants are recruited remotely via electronic medical records and patient registries.

## Eligibility

Adults aged 18–80 with symptomatic SARS-CoV-2 infection confirmed within 5 days, no contraindications to study antiviral. Exclusions follow standard precedent and include pregnancy and severe renal impairment.

## Sample Size

Target n=2,400 (1,200 per arm) provides 80% power to detect a 15% absolute reduction in 12-symptom RECOVER score [@thaweethai2023] at week 12, alpha=0.05 two-sided, assuming 20% attrition.

<!--mcq:sample_size-->

## Outcomes

Primary outcome: 12-symptom RECOVER score at week 12. Secondary outcomes include EQ-5D-5L, return-to-work status, and adverse events.""",
    )

    store.upsert_page(
        "results",
        "Results",
        """## Anticipated Findings

If the early-antiviral arm shows statistically significant reduction in the 12-symptom score, this trial will join an emerging body of evidence (e.g., recent disease-modifying immunotherapy trials such as lecanemab for Alzheimer's disease [@vandyck2023]) demonstrating that early therapeutic intervention can modify chronic disease trajectories.

## Subgroup Pre-Specifications

Pre-specified subgroups: age (<50, ≥50), sex, vaccination status, baseline symptom cluster.

<!--mcq:multiplicity-->

## Sensitivity Analyses

Per ICH E9(R1) §A.4 estimands framework, the primary analysis uses a treatment-policy strategy. A sensitivity analysis using a hypothetical strategy (where intercurrent events are imagined absent) will be conducted via reference-based imputation.""",
    )

    # ---- MCQs ----------------------------------------------------------
    store.upsert_mcq(
        "mcq_framing", "introduction",
        "How should the introduction frame the trial — as a pragmatic effectiveness study, or a mechanism-elucidation study?",
        [
            "Pragmatic effectiveness — primary audience is policymakers and payers",
            "Mechanism — primary audience is biomedical investigators",
            "Hybrid — single-paragraph mechanism, then pragmatic framing",
        ],
        anchor="framing",
    )
    store.upsert_mcq(
        "mcq_sample_size", "methods",
        "For the sample-size calculation, which assumption set fits your reviewers best?",
        [
            "15% absolute reduction (current draft) — moderate, defensible",
            "10% absolute reduction — more conservative, n increases to ~3,800",
            "20% absolute reduction — bold, n decreases to ~1,500",
        ],
        anchor="sample_size",
    )
    store.upsert_mcq(
        "mcq_multiplicity", "results",
        "Subgroup analyses — adjust for multiplicity?",
        [
            "Hierarchical gatekeeping (Bretz 2009 graphical) for primary→secondary, descriptive only for subgroups",
            "Bonferroni across all 4 prespecified subgroups",
            "Benjamini–Hochberg FDR for the subgroup family only",
        ],
        anchor="multiplicity",
    )

    store.set_kv("current_page", "introduction")
