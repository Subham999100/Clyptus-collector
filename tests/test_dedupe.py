from collector.pipelines.dedupe import Deduper, dedupe
from collector.utils.identity import job_key
from tests.conftest import make_record


def test_duplicate_in_one_run_reported_first_preserved() -> None:
    first = make_record(apply_url="https://a.example/1", location="First")
    second = make_record(apply_url="https://a.example/1?utm_source=x", location="Second")

    result = dedupe([first, second])

    assert result.accepted == [first]
    assert len(result.duplicates) == 1
    dup = result.duplicates[0]
    assert dup.record is second
    assert dup.first is first
    assert dup.job_key == job_key(first)


def test_same_title_different_jobs_not_merged() -> None:
    a = make_record(title="Engineer", apply_url="https://a.example/1")
    b = make_record(title="Engineer", apply_url="https://a.example/2")
    c = make_record(title="Engineer", external_id="9")

    result = dedupe([a, b, c])

    assert result.accepted == [a, b, c]
    assert result.duplicates == []


def test_duplicate_by_external_id_with_different_urls() -> None:
    a = make_record(external_id="7", apply_url="https://a.example/1")
    b = make_record(external_id="7", apply_url="https://a.example/other")

    result = dedupe([a, b])

    assert result.accepted == [a]
    assert [d.record for d in result.duplicates] == [b]


def test_three_copies_all_point_at_first() -> None:
    records = [make_record(location=str(i)) for i in range(3)]
    result = dedupe(records)
    assert result.accepted == [records[0]]
    assert all(d.first is records[0] for d in result.duplicates)
    assert len(result.duplicates) == 2


def test_empty_input() -> None:
    result = dedupe([])
    assert result.accepted == [] and result.duplicates == []


def test_deduper_is_incremental_and_per_instance() -> None:
    record = make_record()
    deduper = Deduper()
    assert deduper.add(record) is None
    assert deduper.add(make_record()) is not None
    assert Deduper().add(record) is None  # a new run starts fresh
