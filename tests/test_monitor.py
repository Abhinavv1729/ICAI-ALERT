from monitor import batch_key, parse_batch_html, target_option

SAMPLE = '''
<html><body>
<table><tr><th>Region</th><th>POU</th><th>Course</th></tr></table>
<table id="ctl00_ContentPlaceHolder1_gvBatchDetails">
<tr><th>Batch Number</th><th>Start Date</th><th>End Date</th><th>Seats Available</th></tr>
<tr><td>ICITSSOC__NASHIK_15</td><td>24/06/2024</td><td>11/07/2024</td><td>30</td></tr>
</table>
</body></html>'''


def test_parse_only_grid():
    rows, confident = parse_batch_html(SAMPLE)
    assert confident
    assert len(rows) == 1
    assert rows[0]["fields"]["Batch Number"] == "ICITSSOC__NASHIK_15"


def test_seat_changes_do_not_trigger_alert():
    one, _ = parse_batch_html(SAMPLE)
    two, _ = parse_batch_html(SAMPLE.replace(">30<", ">5<"))
    assert batch_key("ICITSS OC", one[0]) == batch_key("ICITSS OC", two[0])


def test_empty_results():
    rows, confident = parse_batch_html("<html><body>No Records Found</body></html>")
    assert confident and rows == []


def test_unexpected_markup_is_not_accepted():
    rows, confident = parse_batch_html("<html><body>Launch Batch Details</body></html>")
    assert rows == [] and not confident


def test_option_selection():
    opts = [{"label": "--Select--", "value": "", "disabled": False},
            {"label": "Western Region", "value": "W", "disabled": False},
            {"label": "Eastern Region", "value": "E", "disabled": False}]
    assert target_option(opts, "region")["value"] == "W"
    opts = [{"label": "NASHIK BRANCH", "value": "21", "disabled": False}]
    assert target_option(opts, "pou")["value"] == "21"
