from pathlib import Path
from submitters.field_extractor import parse_fields

HTML = (Path(__file__).parent / "fixtures" / "easy_apply_step.html").read_text(encoding="utf-8")


def test_parses_all_field_kinds():
    fields = parse_fields(HTML)
    kinds = {f.kind for f in fields}
    assert "text" in kinds and "number" in kinds and "radio" in kinds and "file" in kinds
    yrs = next(f for f in fields if "years" in f.label.lower())
    assert yrs.required is True
    auth = next(f for f in fields if "authorized" in f.label.lower())
    assert set(o.lower() for o in auth.options) == {"yes", "no"}
