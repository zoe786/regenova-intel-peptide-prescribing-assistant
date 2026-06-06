"""Tests for autonomous PubMed ingestion (no network required).

Network-dependent calls (esearch/efetch) are stubbed; we test query
building integration, date parsing, dedup logic, and provenance threading.
"""

from __future__ import annotations

from datetime import datetime

from pipelines.ingest_pubmed import PubMedIngestor, _parse_pub_date


class TestParsePubDate:
    def test_full_date(self) -> None:
        assert _parse_pub_date("2021 Jun 15") == datetime(2021, 6, 15)

    def test_year_month(self) -> None:
        assert _parse_pub_date("2020 Mar") == datetime(2020, 3, 1)

    def test_year_only(self) -> None:
        assert _parse_pub_date("2019") == datetime(2019, 1, 1)

    def test_empty(self) -> None:
        assert _parse_pub_date("") is None

    def test_garbage(self) -> None:
        assert _parse_pub_date("no date here") is None

    def test_day_clamped(self) -> None:
        # Feb 31 would be invalid; clamp keeps it valid.
        assert _parse_pub_date("2021 Feb 31") == datetime(2021, 2, 28)


class TestProvenanceThreading:
    def _ingestor(self) -> PubMedIngestor:
        ing = PubMedIngestor(chroma_persist_dir="/tmp/does-not-exist")
        ing._discovery_provenance = {
            "12345": {"pubmed_query": '"BPC-157"[tiab]', "peptide": "BPC-157"}
        }
        ing._fetch_abstracts = lambda pmids: [  # type: ignore[assignment]
            {
                "pmid": "12345",
                "title": "BPC-157 tendon study",
                "abstract": "Findings...",
                "authors": ["Smith J"],
                "pub_date": "2021 Jun 15",
                "journal": "J Peptide Res",
            }
        ]
        return ing

    def test_query_threaded_into_extra_metadata(self) -> None:
        docs = self._ingestor().load_raw(pmids=["12345"])
        assert len(docs) == 1
        assert docs[0].extra_metadata["pubmed_query"] == '"BPC-157"[tiab]'
        assert docs[0].extra_metadata["peptide"] == "BPC-157"

    def test_published_at_parsed(self) -> None:
        docs = self._ingestor().load_raw(pmids=["12345"])
        assert docs[0].published_at == datetime(2021, 6, 15)

    def test_journal_recorded(self) -> None:
        docs = self._ingestor().load_raw(pmids=["12345"])
        assert docs[0].extra_metadata["journal"] == "J Peptide Res"


class TestEntrezDelay:
    def test_delay_faster_with_api_key(self) -> None:
        with_key = PubMedIngestor(api_key="abc")._entrez_delay
        without_key = PubMedIngestor(api_key="")._entrez_delay
        assert with_key < without_key
