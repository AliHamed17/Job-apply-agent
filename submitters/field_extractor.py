"""Parse Easy Apply modal HTML into FieldSpec objects (browser-free)."""

from __future__ import annotations

from bs4 import BeautifulSoup

from submitters.form_brain import FieldSpec


def _label_for(soup, el) -> str:
    eid = el.get("id")
    if eid:
        lab = soup.find("label", attrs={"for": eid})
        if lab and lab.get_text(strip=True):
            return lab.get_text(strip=True)
    if el.get("aria-label"):
        return el["aria-label"]
    fs = el.find_parent("fieldset")
    if fs and fs.find("legend"):
        return fs.find("legend").get_text(strip=True)
    return el.get("name", "")


def parse_fields(html: str) -> list[FieldSpec]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[FieldSpec] = []
    seen_radio_groups: set[str] = set()

    for el in soup.find_all(["input", "textarea", "select"]):
        typ = str(el.get("type") or el.name or "text").lower()
        if typ == "hidden" or el.has_attr("disabled"):
            continue
        required = el.has_attr("required") or el.get("aria-required") == "true"

        if el.name == "textarea":
            out.append(FieldSpec(_label_for(soup, el), "textarea", [], required))
            continue
        if el.name == "select":
            opts = [o.get_text(strip=True) for o in el.find_all("option")]
            out.append(FieldSpec(_label_for(soup, el), "select", opts, required))
            continue
        if typ == "file":
            out.append(FieldSpec(_label_for(soup, el), "file", [], required))
            continue
        if typ == "number":
            out.append(FieldSpec(_label_for(soup, el), "number", [], required))
            continue
        if typ in ("radio", "checkbox"):
            fs = el.find_parent("fieldset")
            legend = fs.find("legend") if fs else None
            group = legend.get_text(strip=True) if legend else str(el.get("name") or "")
            if group in seen_radio_groups:
                continue
            seen_radio_groups.add(group)
            opts = []
            scope = fs or soup
            for r in scope.find_all("input", attrs={"type": typ}):
                lab = _label_for(soup, r)
                if lab:
                    opts.append(lab)
            out.append(
                FieldSpec(
                    group, typ, opts, required or (fs.has_attr("aria-required") if fs else False)
                )
            )
            continue
        # default text
        out.append(FieldSpec(_label_for(soup, el), "text", [], required))
    return out
