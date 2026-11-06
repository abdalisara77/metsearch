import json

import httpx
import pytest
import respx

from metsearch.cli import EXIT_FAILURE, EXIT_OK, EXIT_TOO_MANY, EXIT_USAGE, main
from metsearch.client import BASE_URL

SEARCH_URL = f"{BASE_URL}/v1.1/search"


def search_handler(ids, total=None):
    def handler(request):
        return httpx.Response(
            200, json={"total": total if total is not None else len(ids), "objectIDs": ids}
        )

    return handler


def mock_object(object_id, end):
    respx.get(f"{BASE_URL}/v1/objects/{object_id}").respond(
        json={
            "objectID": object_id,
            "objectBeginDate": end,
            "objectEndDate": end,
            "objectDate": str(end),
            "title": f"Work {object_id}",
            "artistDisplayName": "An Artist",
            "objectURL": f"https://example.org/{object_id}",
        }
    )


@pytest.fixture
def db(tmp_path):
    return ["--db", str(tmp_path / "cache.db")]


@respx.mock
def test_prints_table_and_summary(db, capsys):
    respx.get(SEARCH_URL).mock(side_effect=search_handler([1, 2]))
    mock_object(1, 1900)
    mock_object(2, 2000)
    assert main(["tile", *db]) == EXIT_OK
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0].split() == ["End", "date", "Title", "Artist", "URL"]
    assert "Work 2" in lines[2]
    assert "Work 1" in lines[3]
    assert "2 matches" in captured.err


@respx.mock
def test_json_output_holds_only_results(db, capsys):
    respx.get(SEARCH_URL).mock(side_effect=search_handler([1]))
    mock_object(1, 1900)
    assert main(["tile", "--json", *db]) == EXIT_OK
    assert json.loads(capsys.readouterr().out) == [
        {
            "objectID": 1,
            "found": True,
            "objectEndDate": 1900,
            "objectDate": "1900",
            "title": "Work 1",
            "artist": "An Artist",
            "url": "https://example.org/1",
        }
    ]


@respx.mock
def test_id_with_no_record_is_shown_as_no_match(db, capsys):
    respx.get(SEARCH_URL).mock(side_effect=search_handler([1, 2]))
    mock_object(1, 1900)
    respx.get(f"{BASE_URL}/v1/objects/2").respond(404, json={"message": "not found"})
    assert main(["tile", *db]) == EXIT_OK
    captured = capsys.readouterr()
    last_row = captured.out.splitlines()[-1]
    assert "(no match)" in last_row
    assert "objectID 2" in last_row
    assert "1 unusable" in captured.err


@respx.mock
def test_json_marks_id_with_no_record(db, capsys):
    respx.get(SEARCH_URL).mock(side_effect=search_handler([2]))
    respx.get(f"{BASE_URL}/v1/objects/2").respond(404, json={"message": "not found"})
    assert main(["tile", "--json", *db]) == EXIT_OK
    result = json.loads(capsys.readouterr().out)[0]
    assert (result["objectID"], result["found"], result["objectEndDate"]) == (2, False, None)


@respx.mock
def test_defaults_to_tessellation(db):
    route = respx.get(SEARCH_URL).mock(side_effect=search_handler([]))
    assert main(db) == EXIT_OK
    assert route.calls[0].request.url.params["q"] == "tessellation"


@respx.mock
def test_no_results_exits_zero(db, capsys):
    respx.get(SEARCH_URL).mock(side_effect=search_handler([]))
    assert main(["nothing", *db]) == EXIT_OK
    assert "No results." in capsys.readouterr().out


@respx.mock
def test_too_many_results_exit_code(db, capsys):
    respx.get(SEARCH_URL).mock(side_effect=search_handler([1], total=10_001))
    assert main(["art", *db]) == EXIT_TOO_MANY
    assert "10,001" in capsys.readouterr().err


@respx.mock
def test_firewall_block_exit_code(db, capsys):
    respx.get(SEARCH_URL).respond(403, text="<html>blocked</html>")
    assert main(["tile", *db]) == EXIT_FAILURE
    assert "wait before trying again" in capsys.readouterr().err


@pytest.mark.parametrize(
    "bad_args",
    [["--limit", "0"], ["--rate", "81"], ["--rate", "0"], ["   "]],
)
def test_invalid_arguments(db, bad_args):
    with pytest.raises(SystemExit) as exit_info:
        main([*bad_args, *db])
    assert exit_info.value.code == EXIT_USAGE
