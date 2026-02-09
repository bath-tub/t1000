from j2pr.footer import parse_footer


def test_parse_footer() -> None:
    line = (
        'J2PR_RESULT: {"decision":"proceed","summary":"ok","changes":["a"],'
        '"tests":{"command":"pytest","result":"pass","notes":""},"risk":"low",'
        '"repo":"repo","branch":"branch","commit_message":"msg","notes_for_reviewer":"",'
        '"blocking_reason":""}'
    )
    footer = parse_footer(line)
    assert footer is not None
    assert footer.decision == "proceed"
