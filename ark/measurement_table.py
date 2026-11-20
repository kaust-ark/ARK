"""Select, typeset and plot columns directly from a sealed worker measurement."""

from decimal import Decimal, InvalidOperation
import math
import re
import statistics


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


def planned_columns(value):
    """Validate the result table a researcher names before submitting a worker."""
    if not isinstance(value, dict) or value.keys() != {"rows_pointer", "columns"}:
        raise ValueError("Register the distinguishing measurement with rows_pointer and columns.")
    pointer, columns = value["rows_pointer"], value["columns"]
    if (not isinstance(pointer, str) or len(pointer) > 320
            or not re.fullmatch(r"(?:/(?:[^~/]|~[01])*)*", pointer)):
        raise ValueError("Use a JSON pointer to a result table, or an empty string for the root array.")
    if (not isinstance(columns, list) or not 1 <= len(columns) <= 16 or
            not all(isinstance(c, str) and len(c) <= 120 and re.fullmatch(r"[\w-]+(?:\.[\w-]+)*", c)
                    for c in columns) or len(set(columns)) != len(columns)):
        raise ValueError("Register one to sixteen distinct numeric result columns using dotted field paths.")
    return {"rows_pointer": pointer, "columns": list(columns)}


def experiment_design(plan, data):
    """Compare the pre-submission design to actual worker rows. Values do not adjudicate a hypothesis."""
    design = {key: plan[key] for key in ("question", "baseline", "comparison", "metric")}
    mechanism = plan.get("mechanism")
    if not mechanism:
        return design
    design["mechanism"] = mechanism
    if "measurement" not in mechanism:
        design["observation"] = {"status": "unspecified"}
        return design
    spec = mechanism["measurement"]
    rows = data
    for key in spec["rows_pointer"].split("/")[1:]:
        key = key.replace("~1", "/").replace("~0", "~")
        if isinstance(rows, dict):
            rows = rows.get(key)
        elif isinstance(rows, list) and re.fullmatch(r"(?:0|[1-9][0-9]{0,8})", key) and int(key) < len(rows):
            rows = rows[int(key)]
        else:
            rows = None
            break
    result = {"status": "missing"}
    if isinstance(rows, list) and rows:
        complete, any_value = 0, False
        for row in rows:
            available = 0
            for column in spec["columns"]:
                try:
                    value = _value(row, column)
                except ValueError:
                    continue
                if type(value) is int or (type(value) is float and math.isfinite(value)):
                    available += 1
            complete += available == len(spec["columns"])
            any_value |= available > 0
        result = {"status": "recorded" if complete == len(rows) else "partial" if any_value else "missing",
                  "count": complete, "total": len(rows)}
    design["observation"] = result
    return design


def unchanged_result_arrays(previous, current):
    """Compare like-shaped rows in their recorded order, preserving labels and integer identifiers."""
    def matches(a, b):
        if type(a) is dict and type(b) is dict:
            return a.keys() == b.keys() and all(matches(a[k], b[k]) for k in a)
        if type(a) is list and type(b) is list:
            return len(a) == len(b) and all(matches(x, y) for x, y in zip(a, b))
        if type(a) is float and type(b) is float:
            return math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=1e-12, abs_tol=0)
        return type(a) is type(b) and a == b

    old = project_rows(previous)
    return [{"rows_pointer": path, "rows": len(rows)} for path, rows in project_rows(current).items()
            if path in old and matches(old[path], rows)]


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


ROW_IDENTIFIERS = {"condition", "label", "seed", "fold", "split", "trial", "run", "replicate",
                   "condition_id", "fold_id", "split_id", "trial_id", "run_id", "sample_id"}


def summarize(data, rows_pointer, column, reference=None, condition_columns=None):
    """Calculate descriptive statistics over the same sealed rows, paired by row."""
    rows = _rows(data, rows_pointer)

    def check_path(path):
        if not isinstance(path, str) or len(path) > 120 or not re.fullmatch(r"[\w-]+(?:\.[\w-]+)*", path):
            raise ValueError("Choose a column listed for this result table.")

    def series(path):
        check_path(path)
        numbers = [_value(row, path) for row in rows]
        if not all(type(n) in (int, float) and math.isfinite(n) for n in numbers):
            raise ValueError(f"Column {path} must contain a finite number in every selected row.")
        return numbers

    def describe_series(values):
        return {"mean": statistics.mean(values), "min": min(values), "max": max(values),
                "sample_sd": statistics.stdev(values) if len(values) > 1 else None}

    values = series(column)
    result = {"n": len(rows), "value": describe_series(values)}
    if reference is None:
        if condition_columns is not None:
            raise ValueError("A per-condition comparison requires a reference.")
        return result
    if reference == column:
        raise ValueError("Choose two different columns for a paired contrast.")
    other = series(reference)
    differences = [v - r for v, r in zip(values, other)]
    if not all(math.isfinite(d) for d in differences):
        raise ValueError("A paired difference exceeds the numeric range.")
    result["reference"] = describe_series(other)
    result["paired_difference"] = describe_series(differences)
    baseline = result["reference"]["mean"]
    if baseline:
        result["percent_change_in_means"] = 100 * result["paired_difference"]["mean"] / baseline

    if condition_columns is not None:
        if not isinstance(condition_columns, list) or not 1 <= len(condition_columns) <= 4:
            raise ValueError("Supply one to four condition columns.")
        for path in condition_columns:
            check_path(path)
        if len({column, reference, *condition_columns}) != 2 + len(condition_columns):
            raise ValueError("Select distinct outcome, reference and condition columns.")
        comparisons = []
        seen = set()
        for row, value, ref, delta in zip(rows, values, other, differences):
            condition = {p: _value(row, p) for p in condition_columns}
            for item in condition.values():
                if not (type(item) in (int, float) and math.isfinite(item)
                        or isinstance(item, str) and 0 < len(item) <= 100 and item.isprintable()):
                    raise ValueError("Each condition needs a finite number or a short printable label.")
            identity = tuple(condition.values())
            if identity in seen:
                raise ValueError("Choose condition columns that uniquely identify each row, including repeats.")
            seen.add(identity)
            denominator = abs(ref)
            fraction = (denominator - abs(value)) / denominator if denominator else None
            if fraction is not None and not math.isfinite(fraction):
                raise ValueError("Fractional change exceeds the numeric range for this condition.")
            comparisons.append({"condition": condition, "column": column, "value": value,
                                "reference_column": reference, "reference": ref, "signed_difference": delta,
                                "reference_magnitude": denominator, "fraction_reduction_in_magnitude": fraction})
        result["per_condition"] = comparisons
    return result


def reproduction_row(data, unchanged, digest):
    """Choose a real condition and all of its finite scalar outcomes for independent replay."""
    arrays = project_rows(data)
    for item in unchanged:
        pointer = item["rows_pointer"]
        rows = arrays.get(pointer, [])
        if not rows:
            continue
        # Use the same condition identifiers as the paper tables. The reviewer
        # cannot pick an easy hand-chosen case or a subset of the outcomes.
        keys = [p for p in _leaf_paths(rows[0]) if p.split(".")[-1].lower() in ROW_IDENTIFIERS]
        row = rows[int(digest[:12], 16) % len(rows)]
        identifiers = {p: _value(row, p) for p in keys if _has_scalar_id(row, p)}
        if len(rows) > 1 and (not identifiers or sum(all(_has_scalar_id(other, p) and _value(other, p) == v
                                  for p, v in identifiers.items()) for other in rows) != 1):
            continue
        values = {p: _value(row, p) for p in _leaf_paths(row) if p not in keys
                  and type(_value(row, p)) in (int, float) and math.isfinite(_value(row, p))}
        if 1 <= len(values) <= 16 and len(identifiers) <= 8:
            return {"rows_pointer": pointer, "identifiers": identifiers, "outcomes": values}


def _has_scalar_id(row, path):
    try:
        value = _value(row, path)
    except ValueError:
        return False
    return type(value) is int or isinstance(value, str) and 0 < len(value) <= 100


def with_row_identifiers(data, pointer, columns):
    """Keep measured conditions identifiable when typesetting per-trial rows."""
    rows = _rows(data, pointer)
    if len(rows) < 2 or not isinstance(columns, list):
        return columns
    selected = {col["path"] for col in columns if isinstance(col, dict) and isinstance(col.get("path"), str)}
    keys = []
    for path in _leaf_paths(rows[0]):
        name = path.split(".")[-1].lower()
        if name not in ROW_IDENTIFIERS:
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


# Table headings may mix text with short inline formulas. Only these LaTeX
# commands can enter math mode; result values always stay escaped text.
_MATH_SYMBOLS = set(
    (r"\alpha \beta \gamma \delta \epsilon \varepsilon \zeta \eta \theta \vartheta \iota \kappa "
     r"\lambda \mu \nu \xi \pi \varpi \rho \varrho \sigma \varsigma \tau \upsilon \phi \varphi "
     r"\chi \psi \omega \Gamma \Delta \Theta \Lambda \Xi \Pi \Sigma \Upsilon \Phi \Psi \Omega "
     r"\pm \times \cdot \leq \geq \approx \infty \log \exp \min \max").split()
)
_MATH_FORMATS = {r"\bar", r"\hat", r"\tilde", r"\overline", r"\mathrm", r"\mathbf", r"\mathit", r"\mathsf", r"\frac"}


def _valid_header_math(formula):
    # Parsing atoms and their scripts also prevents a trailing subscript or
    # unclosed command argument from consuming the next table cell.
    tokens = re.findall(r"\\[A-Za-z]+|[^ ]", formula)[::-1]

    def atom():
        token = tokens.pop() if tokens else ""
        if token == "{":
            return terms() and bool(tokens) and tokens.pop() == "}"
        if token in _MATH_FORMATS:
            return all(tokens and tokens[-1] == "{" and atom() for _ in range(2 if token == r"\frac" else 1))
        return token in _MATH_SYMBOLS or bool(re.fullmatch(r"[A-Za-z0-9*()+.,/=<>|\[\]-]", token))

    def terms():
        if not tokens or tokens[-1] == "}":
            return False
        while tokens and tokens[-1] != "}":
            if not atom():
                return False
            scripts = set()
            while tokens and tokens[-1] in ("_", "^"):
                op = tokens.pop()
                if op in scripts or not atom():
                    return False
                scripts.add(op)
        return True

    return terms() and not tokens


def _legacy_header_tex(label):
    """Reproduce the previous serializer only for identifying frozen table bytes."""
    label = label.replace(r"\%", "%")
    short_math = re.fullmatch(r"\$([A-Za-z0-9 _^*()+./=+-]{1,32})\$", label)
    return r"\(" + short_math[1] + r"\)" if short_math else _tex(label)


def _header_tex(label):
    label = label.replace(r"\%", "%")
    parts = re.split(r"(?<!\\)\$", label)
    formulas = parts[1::2]
    if len(parts) % 2 == 0 or any(not _valid_header_math(part) for part in formulas):
        return _tex(label)
    # A pair of prices such as "$5 to $10" uses dollar signs as text.
    if (parts[0] or parts[-1]) and any(part.strip() != part for part in formulas):
        return _tex(label)
    return "".join(r"\(" + part + r"\)" if i % 2 else _tex(part) for i, part in enumerate(parts))


_TABULAR = re.compile(r"\\begin\{tabular(?P<star>\*)?\}\s*(?(star)\{[^{}]*\}\s*)"
                      r"(?:\[[tbc]\]\s*)?\{(?:[^{}]|\{[^{}]*\})*\}(.*?)\\end\{tabular(?(star)\*)\}", re.S)
_PRINTED_NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def _plain_cell(tex):
    tex = re.sub(r"\\(?:textbf|textit|mathrm|mathbf|num)\{([^{}]*)\}", r"\1", tex.strip())
    tex = re.sub(r"\\([%_&#])", r"\1", tex)
    return " ".join(tex.strip("$").removeprefix(r"\(").removesuffix(r"\)").split())


def _compare_printed_rows(headers, body, data, table):
    columns = {_plain_cell(col.get("label") or col["path"]): col for col in table["columns"]}
    # Require a one-to-one schema. A shared condition or metric name alone
    # doesn't identify a projection, especially for summary and ablation tables.
    if len(columns) != len(table["columns"]) or len(set(headers)) != len(headers) or set(headers) != columns.keys():
        return None
    ids = [i for i, h in enumerate(headers) if columns[h]["path"].split(".")[-1].lower() in ROW_IDENTIFIERS]
    if not ids or len(ids) == len(headers) or any(columns[headers[i]].get("percent") for i in ids):
        return None
    rows = _rows(data, table.get("rows_pointer", ""))
    numeric_ids = {i for i in ids if all(type(_value(row, columns[headers[i]]["path"])) is int for row in rows)}
    by_condition = {}
    for row in rows:
        if not all(_has_scalar_id(row, columns[headers[i]]["path"]) for i in ids):
            return None
        key = tuple(_value(row, columns[headers[i]]["path"]) for i in ids)
        if key in by_condition:
            return None
        by_condition[key] = row
    seen = set()
    conflicts = []
    matched = False
    for cells in body:
        if len(cells) != len(headers):
            return None
        key = tuple(Decimal(cells[i].replace(",", "")) if i in numeric_ids and _PRINTED_NUMBER.fullmatch(cells[i])
                    else cells[i] for i in ids)
        if key not in by_condition or key in seen:
            return None
        seen.add(key)
        before = len(conflicts)
        numeric = False
        for i, (header, printed) in enumerate(zip(headers, cells)):
            if i in ids:
                continue
            col = columns[header]
            value = _value(by_condition[key], col["path"])
            if isinstance(value, str) and value == printed:
                continue
            if type(value) not in (int, float) or not _PRINTED_NUMBER.fullmatch(printed):
                return None
            numeric = True
            places = len(printed.partition(".")[2])
            expected = Decimal(str(value)) * (100 if col.get("percent") else 1)
            if abs(Decimal(printed.replace(",", "")) - expected) > Decimal("0.5").scaleb(-places):
                identity = ", ".join(f"{headers[j]}={cells[j]}" for j in ids)
                conflicts.append(f"{identity}, {header} prints {printed} (worker {expected:.{places}f})")
        matched |= numeric and len(conflicts) == before
    # Without a matching measured row this may be another experiment using
    # the same headers and condition labels. Leave that attribution to review.
    return conflicts if matched and len(seen) >= 2 else []


def transcription_issues(data, tables, sources, pdf):
    """Check visible, hand-entered copies of compiled worker projections against raw rows."""
    if not tables or not pdf.is_file():
        return []
    # Different filenames, column orders or display precision can still
    # identify the same worker projection.
    projections = {}
    for table in tables:
        schema = sorted((_plain_cell(c.get("label") or c["path"]), c["path"], c.get("percent", False))
                        for c in table["columns"])
        projections[table.get("rows_pointer", ""), tuple(schema)] = table
    pages = None
    issues = []
    for name, source in sources.items():
        for match in _TABULAR.finditer(source):
            rows = []
            for row in re.split(r"\\\\(?:\[[^\[\]]*\])?", match[2]):
                row = re.sub(r"\\(?:toprule|midrule|bottomrule|hline)\b", "", row).strip()
                if row:
                    rows.append([_plain_cell(c) for c in re.split(r"(?<!\\)&", row)])
            if len(rows) < 3:
                continue
            candidates = [(table, conflicts) for table in projections.values()
                          if (conflicts := _compare_printed_rows(rows[0], rows[1:], data, table)) is not None]
            if len(candidates) != 1 or not candidates[0][1]:
                continue
            if pages is None:
                import fitz

                with fitz.open(pdf) as doc:
                    pages = [" " + " ".join(p.get_text().split()) + " " for p in doc]
            # Compiling an input file does not prove every literal table in it
            # was typeset (e.g. commented drafts and conditional branches).
            printed = " " + " ".join(cell for row in rows for cell in row) + " "
            page = next((i for i, text in enumerate(pages, 1) if printed in text), None)
            if page is None:
                continue
            table, conflicts = candidates[0]
            line = source.count("\n", 0, match.start()) + 1
            issues.append(f"Table in {name}:{line} (PDF p. {page}) disagrees with the worker measurements. "
                          + ". ".join(conflicts[:4]) + ". Correct the listed cells using the measured rows, "
                          f"or use \\input{{{table['path']}}} for the worker projection.")
    return issues


def render(data, pointer, columns, *, legacy_headers=False):
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
        headers.append(_legacy_header_tex(label) if legacy_headers else _header_tex(label))
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
