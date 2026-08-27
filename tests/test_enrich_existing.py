from scripts.enrich_existing import _fill_evidence_recaps, _runner_rows


def test_enrichment_uses_entire_dynamic_runner_universe_by_default() -> None:
    payload = {
        "runnerUniverse": [{"mint": f"mint-{index}"} for index in range(80)],
        "runners": [{"mint": "highlight-only"}],
    }

    assert len(_runner_rows(payload)) == 80
    assert _runner_rows(payload)[-1]["mint"] == "mint-79"


def test_enrichment_limit_is_only_an_explicit_override() -> None:
    payload = {"runnerUniverse": [{"mint": f"mint-{index}"} for index in range(72)]}

    assert len(_runner_rows(payload, 20)) == 20
    assert len(_runner_rows(payload, 0)) == 72


def test_enrichment_falls_back_to_highlights_for_legacy_snapshots() -> None:
    payload = {"runners": [{"mint": "legacy"}]}

    assert _runner_rows(payload) == [{"mint": "legacy"}]


def test_evidence_recap_never_pastes_raw_x_copy() -> None:
    result = {"coins": [{
        "symbol": "TEST",
        "xInteractions": [{
            "author": "Trader",
            "summary": "Joined the cult. 9hnu9GsJZbACcK6qB8t8Fss2kHC9BdyDPygTPeK1pump 68 views",
            "url": "https://x.com/example/status/1",
        }],
    }]}

    _fill_evidence_recaps(result)

    lore = result["coins"][0]["lore"]
    assert "Joined the cult" not in lore
    assert "move came" not in lore
    assert "trading chatter rather than a verifiable story" in lore
