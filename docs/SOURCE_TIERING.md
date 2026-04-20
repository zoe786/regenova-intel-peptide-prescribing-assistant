# Source Tiering Framework

## Overview

Every document in REGENOVA-Intel is assigned an evidence tier (1–5) that determines its weight in ranking and confidence calculations. This document defines tier criteria, scoring, and worked examples.

---

## Tier Definitions

### Tier 1 — Peer-Reviewed Research
**Weight: 1.00**

**Definition:** Content published in peer-reviewed scientific journals, preprint servers with peer review, or official systematic review databases.

**Examples:**
- PubMed abstracts (MEDLINE-indexed)
- PubMed Central full-text articles
- Cochrane systematic reviews
- Clinical trial registrations (ClinicalTrials.gov) with results
- Published RCT reports

**Minimum criteria:**
- Published in indexed journal OR formal preprint with DOI
- Author affiliations identifiable
- Methods section present
- Data/results presented

---

### Tier 2 — Clinical Protocol Documents
**Weight: 0.85**

**Definition:** Structured clinical documents authored by identifiable practitioners or organisations, describing protocols, guidelines, or clinical rationale.

**Examples:**
- Clinical protocol PDFs from verified sources
- Prescribing guidelines (non-regulatory)
- Formulary documents
- Expert consensus statements (non-published)
- Practitioner handbook PDFs

**Minimum criteria:**
- Author/organisation identifiable
- Clinical context clear
- Not marketing material

---

### Tier 3 — Educational / Practitioner Content
**Weight: 0.65**

**Definition:** Educational content from practitioners, professional organisations, or reputable health platforms. Not peer-reviewed but structured and authored.

**Examples:**
- Skool course materials
- Practitioner educational website content
- YouTube lecture transcripts from identified clinicians
- Podcast transcripts from credentialled presenters
- Professional organisation blog posts

**Minimum criteria:**
- Author/presenter identifiable
- Content is educational in nature (not anecdotal)
- Source has some accountability (named organisation or individual)

---

### Tier 4 — Community / Practitioner Discussion
**Weight: 0.40**

**Definition:** Discussion content from practitioner communities, forums, or community platforms. May contain valuable experiential knowledge but lacks formal review.

**Examples:**
- Skool community posts
- Peptide-focused forum threads
- Private practitioner group discussions
- Q&A content from practitioner platforms

**Minimum criteria:**
- From a practitioner-focused platform (not general public)
- Post author identifiable (even by username)

---

### Tier 5 — Anecdotal / Unverified
**Weight: 0.15**

**Definition:** User testimonials, general public forum content, anonymous reports, or marketing claims.

**Examples:**
- Reddit threads (general)
- Product review sections
- Testimonial blog posts
- Unverified case reports without clinical detail

**Minimum criteria:** None (lowest tier by default)

---

## Metadata Fields for Tier Assignment

Each document carries the following tier-relevant metadata:

| Field | Type | Description |
|-------|------|-------------|
| `evidence_tier_default` | int (1-5) | Tier assigned at ingest based on source type |
| `source_type` | str | `pubmed`, `document`, `website`, `youtube`, `skool_course`, `skool_community`, `forum` |
| `source_name` | str | Name of the publication, site, or platform |
| `published_at` | datetime | Publication date (for recency boost) |
| `jurisdiction` | str | Geographic jurisdiction if relevant (e.g., `US`, `EU`, `global`) |

---

## Scoring Formula

The overall relevance score for a chunk during ranking is:

```
score = tier_weight(tier) × cosine_similarity × recency_boost(published_at)

where:
  tier_weight = {1: 1.0, 2: 0.85, 3: 0.65, 4: 0.40, 5: 0.15}
  cosine_similarity = vector similarity from ChromaDB (0.0–1.0)
  recency_boost = 1.0 if published within 2 years
                = 0.9 if published within 5 years
                = 0.75 if older than 5 years
                = 1.0 if published_at is unknown
```

Aggregate tier score across top-K results:
```
aggregate = mean(tier_weight(tier_i) for chunk_i in top_k_chunks)
```

---

## Upgrade / Downgrade Criteria

### Tier Upgrade (manual, requires admin)
A source may be upgraded one tier if:
- Subsequent peer review confirms the content
- The author has published peer-reviewed work on the same topic
- The content has been formally endorsed by a recognised clinical body

### Tier Downgrade (automatic or manual)
A source is automatically downgraded if:
- The URL returns 404 (mark as stale, downgrade 1 tier)
- The content is identified as marketing copy (downgrade to Tier 5)
- Retraction notice detected in PubMed feed (downgrade Tier 1 → 5)

---

## Worked Examples

### Example 1: PubMed Abstract
```
source_type: pubmed
source_name: "Journal of Peptide Science"
published_at: 2023-06-15
evidence_tier_default: 1
tier_weight: 1.00
cosine_similarity: 0.87
recency_boost: 1.00
final_score: 1.00 × 0.87 × 1.00 = 0.870
```

### Example 2: Skool Course Module
```
source_type: skool_course
source_name: "Advanced Peptide Protocols Course"
published_at: 2023-11-20
evidence_tier_default: 3
tier_weight: 0.65
cosine_similarity: 0.91
recency_boost: 1.00
final_score: 0.65 × 0.91 × 1.00 = 0.592
```

### Example 3: Forum Thread (older)
```
source_type: forum
source_name: "Peptide Practitioners Forum"
published_at: 2018-03-10
evidence_tier_default: 4
tier_weight: 0.40
cosine_similarity: 0.79
recency_boost: 0.75
final_score: 0.40 × 0.79 × 0.75 = 0.237
```

In this example, despite the forum chunk having higher cosine similarity than the PubMed abstract, it ranks lower due to tier weighting and recency penalty — correct behaviour.
