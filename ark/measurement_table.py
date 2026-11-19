"""Select, typeset and plot columns directly from a sealed worker measurement."""

from decimal import Decimal, InvalidOperation
import math
import re


def project_rows(data):
    """Return small per-condition result arrays, with bounded available fields."""
    paths = {}

    def visit(value, pointer, depth):
        if isinstance(value, dict) and depth < 3:
            for key, item in value.items():
                if isinstance(key, str) and len(key) <= 80:
                    visit(item, pointer + "/" + key.replace("~", "~0").replace("/", "~1"), depth + 1)
        elif (
            isinstance(value, list) and 1 <= len(value) <= 100 and all(isinstance(row, dict) for row in value)
        ):
            paths[pointer] = value

    visit(data, "", 0)
    return paths


def _leaf_paths(row, prefix=""):
    for key, value in row.items():
        if not isinstance(key, str) or not re.fullmatch(r"[\w-]+", key):
            continue
        path = prefix + key
        if isinstance(value, dict) and path.count(".") < 2:
            yield from _leaf_paths(value, path + ".")
        elif type(value) in (str, int, float):
            yield path


def describe(data):
    return [
        {"rows_pointer": pointer, "rows": len(rows), "columns": list(_leaf_paths(rows[0]))[:80]}
        for pointer, rows in project_rows(data).items()
    ]


def _rows(data, pointer):
    rows = project_rows(data).get(pointer) if isinstance(pointer, str) else None
    if rows is None:
        raise ValueError("Select a measured result array from the available rows_pointer values.")
    return rows


def _value(row, path):
    for key in path.split("."):
        if not isinstance(row, dict) or key not in row:
            raise ValueError("Missing measured column: " + path)
        row = row[key]
    return row


def with_row_identifiers(data, pointer, columns):
    """Keep measured conditions identifiable when typesetting per-trial rows."""
    rows = _rows(data, pointer)
    if len(rows) < 2 or not isinstance(columns, list):
        return columns
    selected = {col["path"] for col in columns if isinstance(col, dict) and isinstance(col.get("path"), str)}
    keys = []
    for path in _leaf_paths(rows[0]):
        name = path.split(".")[-1].lower()
        if name not in {"condition", "label", "seed", "fold", "split", "trial", "run", "replicate",
                        "condition_id", "fold_id", "split_id", "trial_id", "run_id", "sample_id"}:
            continue
        try:
            values = [_value(row, path) for row in rows]
        except ValueError:
            continue
        if all(type(v) is int or isinstance(v, str) and 0 < len(v) <= 100 for v in values):
            keys.append((path, values))
    signatures = [tuple(values[i] for path, values in keys if path in selected) for i in range(len(rows))]
    additions = []
    for path, values in keys:
        if len(set(signatures)) == len(rows):
            break
        if path in selected:
            continue
        extended = [(*old, value) for old, value in zip(signatures, values)]
        if len(set(extended)) > len(set(signatures)):
            additions.append({"path": path, "label": path.split(".")[-1].replace("_", " ").capitalize(), "digits": 0})
            signatures = extended
    return additions + columns if len(set(signatures)) == len(rows) else columns


def plot_spec(data, args):
    """Validate the requested fields against every row before reserving compute."""
    kind = args.get("kind")
    if kind not in ("line", "points", "bar"):
        raise ValueError("Choose plot kind line, points, or bar.")
    pointer = args.get("rows_pointer", "")
    rows = _rows(data, pointer)
    series = args.get("series")
    if not isinstance(series, list) or not 1 <= len(series) <= 6:
        raise ValueError("Choose one to six measured y columns in series.")

    def label(text):
        if not isinstance(text, str) or not text.strip() or len(text) > 80 or not text.isprintable():
            raise ValueError("Use a short, printable plot label.")
        return text.strip()

    def column(spec, *, categorical=False):
        path = spec.get("path") if isinstance(spec, dict) else None
        if not isinstance(path, str) or not re.fullmatch(r"[\w-]+(?:\.[\w-]+){0,2}", path):
            raise ValueError("Each plotted column needs a dot-separated measurement path.")
        values = [_value(row, path) for row in rows]
        categories = categorical and all(isinstance(v, str) and 0 < len(v) <= 100 for v in values)
        if not categories:
            try:
                numeric = all(type(v) in (int, float) and math.isfinite(v) for v in values)
            except OverflowError:
                numeric = False
            if not numeric:
                raise ValueError(f"Choose finite numeric values for {path}, or text categories for x.")
        return {"path": path, "label": label(spec.get("label", path.replace(".", " ").replace("_", " ")))}

    x = column(args.get("x"), categorical=True)
    ys = [column(s) for s in series]
    return {"kind": kind, "rows_pointer": pointer, "x": x, "series": ys,
            "y_label": label(args.get("y_label", ys[0]["label"] if len(ys) == 1 else "Measured value"))}


def _tex(text):
    escapes = {"\\": r"\textbackslash{}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(escapes.get(c, "\\" + c if c in "{}&%$#_" else c) for c in text)


def render(data, pointer, columns):
    rows = _rows(data, pointer)
    if not isinstance(columns, list) or not 1 <= len(columns) <= 12:
        raise ValueError("Choose one to twelve columns.")
    headers = []
    specs = []
    for col in columns:
        if not isinstance(col, dict) or not isinstance(col.get("path"), str) or not col["path"]:
            raise ValueError("Each column needs a dot-separated measurement path.")
        label = col.get("label") or col["path"]
        digits = col.get("digits", 2)
        if (
            type(digits) is not int
            or not 0 <= digits <= 8
            or not isinstance(label, str)
            or not 1 <= len(label) <= 80
        ):
            raise ValueError("Use a short column label and zero to eight decimal places.")
        if type(col.get("percent", False)) is not bool:
            raise ValueError("percent must be true or false.")
        label = label.replace(r"\%", "%")
        short_math = re.fullmatch(r"\$([A-Za-z0-9 _^*()+./=+-]{1,32})\$", label)
        headers.append(r"\(" + short_math[1] + r"\)" if short_math else _tex(label))
        specs.append((col["path"], digits, col.get("percent", False)))
    lines = [
        r"\begin{tabular}{@{}" + "r" * len(columns) + "@{}}",
        r"\hline",
        " & ".join(headers) + r" \\",
        r"\hline",
    ]
    for row in rows:
        cells = []
        for path, digits, percent in specs:
            value = _value(row, path)
            if type(value) in (int, float):
                try:
                    number = Decimal(str(value)) * (100 if percent else 1)
                    if not number.is_finite():
                        raise ValueError("Measurements must be finite.")
                    cells.append(f"{number:,.{digits}f}")
                except InvalidOperation as err:
                    raise ValueError("Cannot format this measured value.") from err
            elif isinstance(value, str) and len(value) <= 100 and not percent:
                cells.append(_tex(value))
            else:
                raise ValueError("Choose a scalar result column.")
        lines.append(" & ".join(cells) + r" \\")
    return "\n".join([*lines, r"\hline", r"\end{tabular}", ""])
