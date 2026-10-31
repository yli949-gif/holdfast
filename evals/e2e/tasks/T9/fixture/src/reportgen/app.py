import pathlib


def build_report(out_dir: str = "build") -> str:
    p = pathlib.Path(out_dir)
    p.mkdir(exist_ok=True)
    (p / "report.txt").write_text("report\n")
    return str(p / "report.txt")
