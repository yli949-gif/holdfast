from reportgen.app import build_report


def test_build_report(tmp_path):
    out = build_report(str(tmp_path / "out"))
    assert out.endswith("report.txt")
