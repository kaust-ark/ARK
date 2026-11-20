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


def _table_cells(body):
    rows = []
    for row in re.split(r"\\\\(?:\[[^\[\]]*\])?", body):
        row = re.sub(r"\\(?:toprule|midrule|bottomrule|hline)\b", "", row).strip()
        if row:
            rows.append([_plain_cell(c) for c in re.split(r"(?<!\\)&", row)])
    return rows


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
            rows = _table_cells(match[2])
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


_BOUND_NUMBER = r"[+−-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+−-]?[0-9]{1,3})?"
_ALL_ROWS = r"(?:all (?:the )?(?:(?:tested|measured|recorded|observed) )?(?:conditions|rows|runs|trials|settings|folds|seeds)|every (?:condition|row|run|trial|setting|fold|seed))"
_BOUND_COMPARISONS = {"<": lambda x, b: x < b, "lower than": lambda x, b: x < b,
                      ">": lambda x, b: x > b, "higher than": lambda x, b: x > b,
                      "≤": lambda x, b: x <= b, "≥": lambda x, b: x >= b}
_UNCERTAIN_BOUND = re.compile(r"\b(?:about|around|roughly|approximately|approx\w*|round\w*|displayed|"
                              r"if|when|unless|except|excluding|without|not|never|only|subset|assuming|"
                              r"expected|target|budget|goal|criterion|hypothes\w*|plan\w*|aim\w*|predict\w*|"
                              r"intend\w*|want\w*|whether|will|might|could|would|should)\b|[≈~]", re.I)


def _bound_scope(before, after, identifiers):
    # Accept a complete scope adjoining the assertion. An unparsed qualifier
    # before or after the bound could change which rows it describes.
    fields = "|".join(re.escape(name) for name in identifiers)
    selection = rf"(?P<all>{_ALL_ROWS})"
    if fields:
        numbers = rf"{_BOUND_NUMBER}(?:\s*,\s*{_BOUND_NUMBER})*"
        selection += rf"|(?P<id>{fields})\s*(?:∈\s*\{{(?P<set>{numbers})\}}|=\s*(?P<one>{_BOUND_NUMBER}))"
    scope = rf"(?:across|for|at|over|in)\s+(?:{selection})"
    if re.fullmatch(r"(?:the )?", before, re.I):
        match = re.fullmatch(rf"\s*{scope}", after, re.I)
    elif not after or after == ")":
        match = re.fullmatch(rf"{scope}\s*,?\s*(?:the )?", before, re.I)
        if (not match and after == ")" and re.search(r"\b(?:we|our|us)\b", before, re.I)
                and re.search(r"\b(?:measur(?:e[ds]?|ing)|observ(?:e[ds]?|ing)|record(?:ed)?|found)\b", before, re.I)):
            match = re.search(rf"\b{scope}\s+with (?:[A-Za-z]+ ){{0,5}}(?:precision|uncertainty|variability) \($",
                              before, re.I)
            if match and re.search(r"\b(?:for|at|among|during|under|on|in|within|using|with|over|across|"
                                   r"after|before|from)\b", before[:match.start()], re.I):
                return None
    else:
        return None
    if not match:
        return None
    if match["all"]:
        return "", set()
    return match["id"].casefold(), {Decimal(n.strip().replace("−", "-")) for n in
                                   (match["set"] or match["one"]).split(",")}


def _defined_trend_issues(data, tables, blocks):
    """Recompute an explicitly defined affine numerator divided by a condition-dependent scale."""
    from fractions import Fraction
    from ark.review import _ORDERED_ASSERTION

    issues = []
    for page, block in blocks:
        sentences = re.split(r"(?<=[.!?])\s+", block)
        for definition, claim in zip(sentences, sentences[1:]):
            change = _ORDERED_ASSERTION.search(claim)
            if not change or _UNCERTAIN_BOUND.search(definition + " " + claim):
                continue
            if not re.fullmatch(r"(?:(?:as (?:shown|observed|reported) in (?:the|our) results),\s*)?(?:the|this) ",
                                claim[:change.start()], re.I):
                continue
            axis, subject = change["axis"], change["subject"].split()[0]
            # The next sentence must discuss this same quantity with the visible input.
            along = re.fullmatch(r"(?:\s+alongside (?P<input>¯?[A-Za-z]\w*))?(?:,\s*(?:thereby|effectively|reflecting) [^.;]+)?\.",
                                 claim[change.end():])
            if not along or re.search(r"\b(?:for|under|within|except|among|during|until|between)\b", claim, re.I):
                continue
            formula = re.match(
                rf"(?:The|Our) (?:(?P<method>[\w–-]+) )?{re.escape(subject)}(?: term)?\s*"
                rf"(?:(?:is(?: defined as)?|=)\s*)?(?P<expr>\(\s*(?P<x>[A-Za-z]\w*)\s*(?P<op>[+−-])\s*"
                rf"(?P<offset>[0-9]+(?:\.[0-9]+)?)\s*\)\s*/\s*\(\s*(?P<scale>[0-9]+(?:\.[0-9]+)?)"
                rf"\s*[×·*]?\s*{re.escape(axis)}\s*\))(?=\.$|\s+(?:is|was|functions|denotes|represents)\b)",
                definition)
            if not formula or axis == formula["x"]:
                continue
            symbols = {formula["x"], "¯" + formula["x"]}
            if along["input"]:
                symbols.intersection_update([along["input"]])
            if not symbols:
                continue
            candidates = {}
            for pointer, names, caption in tables:
                if axis not in names:
                    continue
                for sym in symbols.intersection(names):
                    key = (pointer, names[axis], names[sym], sym)
                    documented = not formula["method"] or re.search(r"(?<!\w)" + re.escape(formula["method"]) + r"(?!\w)",
                                                                    caption, re.I)
                    candidates[key] = candidates.get(key, False) or bool(documented)
            if len(candidates) != 1 or not any(candidates.values()):
                continue
            pointer, axis_path, input_path, symbol = next(iter(candidates))
            # A bar denotes a mean. At fixed axis the expression is affine in x, so
            # applying it to the recorded mean gives the mean of the per-trial term.
            if symbol.startswith("¯") and not re.search(r"(?:^|[._])(?:mean|avg|average)(?:[._]|$)", input_path, re.I):
                continue
            rows = _rows(data, pointer)
            pairs = [(_value(row, axis_path), _value(row, input_path)) for row in rows]
            if (len(pairs) < 2 or any(not (type(v) is int or isinstance(v, Decimal) and v.is_finite())
                                      for pair in pairs for v in pair)
                    or len({a for a, _ in pairs}) != len(pairs)):
                continue
            offset = Fraction(formula["offset"]) * (1 if formula["op"] == "+" else -1)
            scale = Fraction(formula["scale"])
            if not scale or any(a == 0 for a, _ in pairs):
                continue
            ordered = sorted(pairs)
            values = [(Fraction(x) + offset) / (scale * Fraction(a)) for a, x in ordered]
            increasing = change["direction"].lower().startswith(("increas", "ris", "grow"))
            if any(y > x if increasing else y < x for x, y in zip(values, values[1:])):
                continue
            def endpoint(i):
                a, x = ordered[i]
                v = values[i]
                return f"{axis}={a}, {input_path}={x}, term={v.numerator / Decimal(v.denominator):.10g}"
            issues.append(f'PDF p. {page} reports “{change[0]}”. Its definition {formula["expr"]} on the sealed '
                          f'rows gives {endpoint(0)} and {endpoint(-1)}. None of the {len(rows)-1} adjacent '
                          f'changes moves in the stated direction.')
    return issues


def measured_bound_issues(data, tables, pdf):
    """Find counterexamples to explicitly scoped upper/lower bounds in the rendered submission."""
    if not tables:
        return []
    import fitz

    with fitz.open(pdf) as doc:
        raw_blocks = [(n, b[4]) for n, page in enumerate(doc, 1) for b in page.get_text("blocks") if b[6] == 0]
    blocks = [(n, " ".join(re.sub(r"(?<=[A-Za-z])-\n(?=[a-z])", "", raw).split())) for n, raw in raw_blocks]
    page_text = {}
    captions = {}
    for page, block in blocks:
        page_text[page] = page_text.get(page, " ") + block + " "
        if re.match(r"Table\s+[0-9]+[.:]", block, re.I):
            captions.setdefault(page, []).append(block)
    # Bind aliases only to verified table recipes whose rows reached the PDF.
    labels = {}
    symbol_tables = []
    post_table_text = []
    for table in tables:
        pointer = table.get("rows_pointer", "")
        cells = _table_cells(_TABULAR.fullmatch(table["rendered_table"].strip())[2])
        body = " " + " ".join(c for row in cells[1:] for c in row) + " "
        pages = [n for n, text in page_text.items() if body in text]
        if not pages:
            continue
        caption = captions.get(pages[0], []) if len(pages) == 1 else []
        caption = caption[0] if len(caption) == 1 else ""
        # In a row-major PDF table, each full row and its ordered headers
        # identify which recorded field the typeset symbol denotes.
        for n, raw in raw_blocks:
            lines = raw.strip().splitlines()
            count = len(table["columns"])
            printed_rows = [cell for row in cells[1:] for cell in row]
            if n not in pages:
                continue
            for start in range(count, len(lines) - len(printed_rows) + 1):
                if lines[start:start + len(printed_rows)] != printed_rows:
                    continue
                headings = lines[start-count:start]
                names = {}
                for name, col in zip(headings, table["columns"]):
                    source = _plain_cell(col.get("label") or col["path"])
                    source = re.sub(r"\\bar\{([A-Za-z])\}", r"¯\1", source)
                    source = re.sub(r"_\{([A-Za-z0-9]+)\}", r"\1", source)
                    if name == source and headings.count(name) == 1 and not col.get("percent"):
                        names[name] = col["path"]
                symbol_tables.append((pointer, names, caption))
                if trailing := lines[start + len(printed_rows):]:
                    post_table_text.append((n, " ".join(trailing)))
        ids = {}
        for col in table["columns"]:
            name = _plain_cell(col.get("label") or col["path"]).casefold()
            if re.fullmatch(r"[\w -]{1,48}", name):
                ids.setdefault(name, set()).add(col["path"])
        ids = {name: next(iter(paths)) for name, paths in ids.items() if len(paths) == 1}
        for col in table["columns"]:
            name = _plain_cell(col.get("label") or col["path"])
            label = re.fullmatch(r"([A-Za-z][\w -]{0,48}?)(?:\s*\(([A-Za-zµμ%]+)\))?", name)
            if label:
                key = label[1].strip().casefold()
                signature = (pointer, col["path"], label[2], col.get("percent", False))
                labels.setdefault(key, {}).setdefault(signature, []).append((ids, caption))
    issues = _defined_trend_issues(data, symbol_tables, blocks + post_table_text)
    for label, choices in labels.items():
        if len(choices) != 1:
            continue
        (pointer, path, header_unit, percent), projections = next(iter(choices.items()))
        ids = {k: v for k, v in projections[0][0].items() if all(p.get(k) == v for p, _ in projections)}
        caption = projections[0][1] if len(projections) == 1 else ""
        rows = _rows(data, pointer)
        values = [_value(row, path) for row in rows]
        if not all(type(v) is int or isinstance(v, Decimal) and v.is_finite() for v in values):
            continue
        relation = re.compile(rf"\b{re.escape(label)}\s*(?:(?:was|is|were|are)\s+)?"
                              rf"(?P<op>≤|≥|<|>|lower than\b|higher than\b)\s*(?P<number>{_BOUND_NUMBER})"
                              rf"(?![\w.,/×^−-])", re.I)
        for page, block in blocks:
            if re.search(r"\b(?:all|following|reported) (?:values|measurements|results) (?:are|were) rounded\b",
                         block, re.I):
                continue
            for sentence in re.split(r"(?<=[.!?])\s+", block):
                sentence = sentence.rstrip(".")
                if _UNCERTAIN_BOUND.search(sentence):
                    continue
                for m in relation.finditer(sentence):
                    before, after = sentence[:m.start()].lstrip(), sentence[m.end():].strip()
                    # Any explicit unit must be bound to the named column. A standard
                    # error/deviation reported with its estimate inherits the estimate's unit.
                    unit = re.match(r"([A-Za-zµμ%]+)(?=\s|\)|$)", after)
                    if unit and unit[1].lower() not in {"across", "for", "at", "over", "in"}:
                        token = unit[1]
                        if percent:
                            known = token == "%"
                        elif header_unit:
                            known = token == header_unit
                        else:
                            unit_text = rf"(?-i:{re.escape(token)})(?!\w)"
                            known = path.endswith("_" + token) or re.search(
                                rf"\b{re.escape(label)}\s*(?:\(\s*(?:in )?|\bin ){unit_text}|"
                                rf"\bin {unit_text},? with (?:its|their) (?:[A-Za-z]+ ){{0,3}}standard "
                                rf"(?:error|deviation)\s*\({re.escape(label)}\)", caption, re.I)
                        if not known:
                            continue
                        after = after[unit.end():].strip()
                    elif percent:
                        continue
                    scope = _bound_scope(before, after, ids)
                    if scope is None:
                        continue
                    field, selected = scope
                    if field:
                        raw = [_value(row, ids[field]) for row in rows]
                        if not all(type(v) is int or isinstance(v, Decimal) and v.is_finite() for v in raw):
                            continue
                        settings = [Decimal(str(v)) for v in raw]
                        if len(set(settings)) != len(rows) or not selected <= set(settings):
                            continue
                    bound = Decimal(m["number"].replace("−", "-"))
                    obeys = _BOUND_COMPARISONS[m["op"].lower()]
                    for i, (row, value) in enumerate(zip(rows, values)):
                        if field and settings[i] not in selected:
                            continue
                        observed = Decimal(value)
                        if percent:
                            sign, digits, e = observed.as_tuple()
                            observed = Decimal((sign, digits, e + 2))
                        if not obeys(observed, bound):
                            fields = [ids[field]] if field else [p for p in ids.values()
                                                               if p.split(".")[-1].lower() in ROW_IDENTIFIERS]
                            where = ", ".join(f"{p}={_value(row, p)}" for p in fields)
                            issues.append(f'PDF p. {page} reports “{m[0]}”. The sealed worker row {pointer}/{i}'
                                          f' ({where or "one-based row " + str(i + 1)}) gives {path} = {observed}'
                                          f'{("%" if percent else "")}. Check the boundary at full precision.')
                            break
    return list(dict.fromkeys(issues))


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
