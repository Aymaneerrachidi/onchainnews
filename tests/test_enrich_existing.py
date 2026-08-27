from scripts.enrich_existing import _runner_rows


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
