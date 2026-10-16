"""Pure, opt-in parser for OPPO Health UI OCR text.

This module does not capture screenshots, call ADB, read files, contact a
network, or integrate with the running AstrBot plugin. The adapter is disabled
by default and accepts one screen capture at a time.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime
import math
import re
from typing import Any, Iterable, Mapping, Sequence


LOCAL_ADAPTER_DEFAULT_ENABLED = False
SOURCE = "oppo_health_ui_ocr"

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WELLNESS_CATEGORY_LABELS = {
    "Excellent": "Excellent",
    "Good": "Good",
    "Moderate": "Moderate",
    "Slow down": "Slow down",
    "状态优秀": "Excellent",
    "状态不错": "Good",
    "状态正常": "Moderate",
    "适当放松": "Slow down",
}
_WELLNESS_CATEGORIES = ("Excellent", "Good", "Moderate", "Slow down")
_CYCLE_LEGEND = ("Period", "Predicted period", "Fertility window", "Ovulation")
_WEEKDAY_ALIASES = {
    "sun": 6, "sunday": 6, "mon": 0, "monday": 0, "tue": 1,
    "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2, "thu": 3,
    "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
}


class AdapterDisabled(RuntimeError):
    """Raised unless a caller explicitly opts into parsing one local screen."""


def _lines(observations: Iterable[str | Mapping[str, Any]]) -> list[str]:
    result: list[str] = []
    for observation in observations:
        text = observation if isinstance(observation, str) else observation.get("text")
        if isinstance(text, str) and text.strip():
            result.append(" ".join(text.replace("\u00a0", " ").split()))
    return result


def _observed_at(value: str | datetime) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("observed_at must be an ISO timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.isoformat()


def _selected_date(value: str | date) -> str:
    parsed = value if isinstance(value, date) else date.fromisoformat(value)
    if isinstance(parsed, datetime):
        raise ValueError("selected_date must be a calendar date")
    return parsed.isoformat()


def _number(value: str) -> int | float:
    numeric = float(value)
    return int(numeric) if numeric.is_integer() else numeric


def _month_context(line: str) -> tuple[int, int] | None:
    match = re.search(r"\b([A-Za-z]{3,9})\s+(20\d{2})\b", line)
    if match:
        month = _MONTHS.get(match.group(1)[:3].lower())
        return (int(match.group(2)), month) if month else None
    match = re.search(r"(?<!\d)(0?[1-9]|1[0-2])\s*[/.-]\s*(20\d{2})(?!\d)", line)
    if match:
        return int(match.group(2)), int(match.group(1))
    match = re.search(r"\b(20\d{2})\s*年\s*(\d{1,2})\s*月\b", line)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        return (year, month) if 1 <= month <= 12 else None
    return None


def _visible_day_matches(lines: Sequence[str], selected: str) -> None:
    chosen = date.fromisoformat(selected)
    found = False
    for line in lines:
        match = re.search(r"\b([A-Za-z]{3,9})\s+(\d{1,2})(?:,|\s)\s*", line)
        if match:
            month = _MONTHS.get(match.group(1)[:3].lower())
            if month and (month, int(match.group(2))) != (chosen.month, chosen.day):
                raise ValueError("selected_date conflicts with the visible page date")
            if month:
                found = True
        match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", line)
        if match and (int(match.group(1)), int(match.group(2))) != (chosen.month, chosen.day):
            raise ValueError("selected_date conflicts with the visible page date")
        if match or re.search(r"\btoday\b", line, re.IGNORECASE):
            found = True
    if not found:
        raise ValueError("the selected day is not visible on the page")


def _box(observation: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    value = observation.get("bbox")
    if isinstance(value, Mapping):
        try:
            return tuple(float(value[key]) for key in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(value, Sequence) and len(value) == 4:
        try:
            return tuple(float(part) for part in value)
        except (TypeError, ValueError):
            return None
    return None


def _rgb(observation: Mapping[str, Any]) -> tuple[float, float, float] | None:
    value = observation.get("sample_rgb")
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 3:
        return None
    try:
        result = tuple(float(part) for part in value)
    except (TypeError, ValueError):
        return None
    if any(part < 0 or part > 255 for part in result):
        return None
    return result


def _classify_cycle_color(
    color: tuple[float, float, float],
    palette: Mapping[str, tuple[float, float, float]],
    minimum_chroma: float,
) -> str | None:
    """Classify one calendar-cell sample against the visible legend colors."""
    if max(color) - min(color) < minimum_chroma:
        return "non_period" if min(color) >= 210 else None
    distances = sorted((math.dist(color, swatch), label)
                       for label, swatch in palette.items())
    if distances[0][0] > 55:
        return None
    if distances[1][0] - distances[0][0] < 12:
        best_axis = palette[distances[0][1]][0] - palette[distances[0][1]][2]
        runner_up_axis = palette[distances[1][1]][0] - palette[distances[1][1]][2]
        color_axis = color[0] - color[2]
        hue_resolves_tie = (
            abs(color_axis) >= 16
            and abs(best_axis) >= 12
            and abs(runner_up_axis) >= 12
            and color_axis * best_axis > 0
            and color_axis * runner_up_axis < 0
        )
        if not hue_resolves_tie:
            return None
    return distances[0][1]


def _weekday_layout(
    observations: Sequence[Mapping[str, Any]],
) -> tuple[list[float], list[int], float]:
    """Return calendar weekday centers, weekday indices, and header bottom."""
    entries: list[tuple[str, float, float, float]] = []
    for observation in observations:
        text = observation.get("text")
        bounds = _box(observation)
        if not isinstance(text, str) or bounds is None:
            continue
        tokens = re.findall(
            r"(?i)(?:sun(?:day)?|mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|"
            r"thu(?:r?s?(?:day)?)?|fri(?:day)?|sat(?:urday)?)|(?<!\w)[SMTWF](?!\w)",
            text,
        )
        if not tokens:
            continue
        x, y, width, height = bounds
        for index, token in enumerate(tokens):
            center_x = x + width * (index + 0.5) / len(tokens)
            normalized = token.casefold()
            if len(normalized) == 1:
                entries.append((normalized.upper(), center_x, y + height / 2, y + height))
            else:
                weekday = _WEEKDAY_ALIASES.get(normalized)
                if weekday is not None:
                    entries.append((str(weekday), center_x, y + height / 2, y + height))

    rows: list[list[tuple[str, float, float, float]]] = []
    for entry in sorted(entries, key=lambda item: item[2]):
        row = next((candidate for candidate in rows
                    if abs(candidate[0][2] - entry[2]) <= 0.025), None)
        if row is None:
            rows.append([entry])
        else:
            row.append(entry)

    candidates = []
    for row in rows:
        row.sort(key=lambda item: item[1])
        tokens = [item[0] for item in row]
        weekday_values = [int(token) for token in tokens] if all(token.isdigit() for token in tokens) else None
        if weekday_values not in (list(range(7)), [6, 0, 1, 2, 3, 4, 5]):
            initials = "".join(tokens)
            if initials == "SMTWTFS":
                weekday_values = [6, 0, 1, 2, 3, 4, 5]
            elif initials == "MTWTFSS":
                weekday_values = [0, 1, 2, 3, 4, 5, 6]
            else:
                continue
        if len(row) == 7 and len({round(item[1], 4) for item in row}) == 7:
            candidates.append((row, weekday_values))
    if len(candidates) != 1:
        raise ValueError("could not verify a single seven-column weekday header")
    row, weekday_values = candidates[0]
    return ([item[1] for item in row], weekday_values,
            max(item[3] for item in row))


def parse_cycle_calendar(
    observations: Iterable[str | Mapping[str, Any]],
    *,
    observed_at: str | datetime,
) -> dict[str, Any]:
    """Read only actual and predicted period markers from the visible month."""
    observations = list(observations)
    items = [item for item in observations if isinstance(item, Mapping)]
    lines = _lines(observations)
    if "Calendar" not in lines or "Cycle" not in lines:
        raise ValueError("expected visible Calendar and Cycle options")
    if not any(label in lines for label in ("Period", "Predicted period")):
        raise ValueError("the cycle calendar legend is not visible")

    months = {_month_context(line) for line in lines}
    months.discard(None)
    if len(months) != 1:
        raise ValueError("a single visible month and year are required")
    year, month = next(iter(months))
    centers, weekdays, header_bottom = _weekday_layout(items)

    palette: dict[str, tuple[float, float, float]] = {}
    for label in _CYCLE_LEGEND:
        swatches = [(_rgb({"sample_rgb": item.get("legend_rgb")}) if item.get("legend_rgb") is not None
                     else _rgb(item)) for item in items
                    if isinstance(item.get("text"), str)
                    and item["text"].casefold() == label.casefold()]
        swatches = [color for color in swatches if color is not None]
        if len(swatches) != 1:
            raise ValueError("the visible cycle legend is incomplete or ambiguous")
        palette[label] = swatches[0]
    for index, first in enumerate(_CYCLE_LEGEND):
        for second in _CYCLE_LEGEND[index + 1:]:
            if math.dist(palette[first], palette[second]) < 18:
                raise ValueError("cycle legend colors are too similar to classify safely")

    first_day = date(year, month, 1)
    days_in_month = calendar.monthrange(year, month)[1]
    first_column = weekdays.index(first_day.weekday())
    row_observations: list[tuple[float, int, int, Mapping[str, Any]]] = []
    for item in items:
        text = item.get("text")
        bounds = _box(item)
        if not isinstance(text, str) or bounds is None or not re.fullmatch(r"\d{1,2}", text.strip()):
            continue
        day_number = int(text)
        if not 1 <= day_number <= days_in_month:
            continue
        x, y, width, height = bounds
        center_x = x + width / 2
        center_y = y + height / 2
        if y + height / 2 <= header_bottom:
            continue
        column = min(range(7), key=lambda index: abs(centers[index] - center_x))
        if abs(centers[column] - center_x) > 0.06:
            continue
        offset = first_column + day_number - 1
        expected_column = offset % 7
        row_index = offset // 7
        if column != expected_column:
            continue
        row_observations.append((center_y, row_index, day_number, item))

    row_groups: list[list[tuple[float, int, int, Mapping[str, Any]]]] = []
    for entry in sorted(row_observations, key=lambda item: item[0]):
        group = next((candidate for candidate in row_groups
                      if abs(candidate[0][0] - entry[0]) <= 0.025), None)
        if group is None:
            row_groups.append([entry])
        else:
            group.append(entry)
    seen_days: dict[int, Mapping[str, Any]] = {}
    for group in row_groups:
        if len({entry[1] for entry in group}) != 1:
            continue
        for _, _, day_number, item in group:
            if day_number in seen_days:
                raise ValueError("calendar day labels are duplicated")
            seen_days[day_number] = item
    expected_rows = list(range((first_column + days_in_month - 1) // 7 + 1))
    ordered_row_groups = [
        group for group in sorted(row_groups, key=lambda values: values[0][0])
        if len({entry[1] for entry in group}) == 1
    ]
    located_rows = [next(iter({entry[1] for entry in group})) for group in ordered_row_groups]
    # OCR may omit individual date numerals; the local collector supplies
    # calibrated cell samples only for the verified emulator viewport.
    grid_items = [
        item.get("calendar_grid_samples") for item in items
        if isinstance(item.get("calendar_grid_samples"), Mapping)
    ]
    grid_valid = False
    grid_available = len(grid_items) == 1
    grid_weekdays_match = False
    grid_rows_match = False
    grid_sample_shape_match = False
    grid_colors: list[list[Any]] = []
    if grid_available:
        grid = grid_items[0]
        try:
            grid_weekdays = [float(value) for value in grid.get("weekday_centers", [])]
            grid_rows = [float(value) for value in grid.get("row_centers", [])]
            grid_colors = grid.get("sample_rgb_candidates", [])
            # OCR boxes for weekday names can be shifted by font width even
            # when the date numerals and calibrated cell centers align. Use
            # visible date numerals, whose weekday is fixed by the month
            # heading, as the geometric anchors for the sampled grid.
            weekday_anchor_errors: list[list[float]] = [[] for _ in range(7)]
            row_anchor_errors: list[list[float]] = [[] for _ in expected_rows]
            for day_number, item in seen_days.items():
                bounds = _box(item)
                if bounds is None:
                    continue
                x, y, width, height = bounds
                offset = first_column + day_number - 1
                row_index, column = offset // 7, offset % 7
                if column >= len(grid_weekdays) or row_index >= len(grid_rows):
                    continue
                center_x, center_y = x + width / 2, y + height / 2
                weekday_anchor_errors[column].append(abs(center_x - grid_weekdays[column]))
                row_anchor_errors[row_index].append(abs(center_y - grid_rows[row_index]))
            weekday_anchor_counts = [len(values) for values in weekday_anchor_errors]
            row_anchor_counts = [len(values) for values in row_anchor_errors]
            date_anchors_align_x = (
                len(grid_weekdays) == 7
                and all(count >= 2 for count in weekday_anchor_counts)
                and all(max(errors) <= 0.02 for errors in weekday_anchor_errors)
            )
            date_anchors_align_y = (
                len(grid_rows) == len(expected_rows)
                and sum(count > 0 for count in row_anchor_counts) >= max(1, len(expected_rows) - 1)
                and all(not errors or max(errors) <= 0.02 for errors in row_anchor_errors)
            )
            grid_weekdays_match = (
                date_anchors_align_x
            )
            calibrated_rows = [472 / 1280, 592 / 1280, 712 / 1280, 832 / 1280, 952 / 1280]
            grid_geometry_matches_screen = (
                len(expected_rows) == len(calibrated_rows)
                and len(grid_rows) == len(calibrated_rows)
                and all(abs(left - right) <= 0.025 for left, right in zip(calibrated_rows, grid_rows))
            )
            grid_rows_match = (
                grid_geometry_matches_screen and date_anchors_align_y
            )
            grid_sample_shape_match = (
                len(grid_colors) == len(expected_rows)
                and all(len(row) == 7 for row in grid_colors)
                and all(
                    isinstance(grid_colors[(first_column + day_number - 1) // 7]
                               [(first_column + day_number - 1) % 7], list)
                    and len(grid_colors[(first_column + day_number - 1) // 7]
                             [(first_column + day_number - 1) % 7]) >= 5
                    for day_number in range(1, days_in_month + 1)
                )
            )
            grid_valid = (
                grid_weekdays_match
                and grid_rows_match
                and grid_sample_shape_match
                and len(seen_days) >= max(7, math.ceil(days_in_month * 0.5))
            )
        except (TypeError, ValueError):
            grid_valid = False

    if grid_valid:
        verified_days: dict[int, Mapping[str, Any]] = {}
        for day_number in range(1, days_in_month + 1):
            offset = first_column + day_number - 1
            row_index, column = offset // 7, offset % 7
            candidates = grid_colors[row_index][column]
            if isinstance(candidates, list) and len(candidates) >= 5:
                verified_days[day_number] = {"sample_rgb_candidates": candidates}
            elif day_number in seen_days:
                verified_days[day_number] = seen_days[day_number]
    else:
        if located_rows != expected_rows:
            raise ValueError("calendar row order could not be verified")
        if len(seen_days) < math.ceil(days_in_month * 0.9):
            raise ValueError("calendar OCR did not cover enough dates to classify safely")
        verified_days = dict(seen_days)

    actual: list[str] = []
    predicted: list[str] = []
    known_non_period: list[str] = []
    classified: list[str] = []
    minimum_chroma = max(
        8.0,
        0.25 * min(
            max(palette["Period"]) - min(palette["Period"]),
            max(palette["Predicted period"]) - min(palette["Predicted period"]),
        ),
    )
    for day_number, item in verified_days.items():
        day_text = date(year, month, day_number).isoformat()
        classification = None
        color_samples = item.get("sample_rgb_candidates")
        if isinstance(color_samples, list):
            votes: dict[str, int] = {}
            for value in color_samples:
                sample = _rgb({"sample_rgb": value}) if isinstance(value, Sequence) else None
                label = _classify_cycle_color(sample, palette, minimum_chroma) if sample else None
                if label:
                    votes[label] = votes.get(label, 0) + 1
            if votes:
                ranked_votes = sorted(votes.items(), key=lambda entry: entry[1], reverse=True)
                required_votes = len(color_samples) // 2 + 1
                if (
                    ranked_votes[0][1] >= required_votes
                    and (len(ranked_votes) == 1 or ranked_votes[0][1] > ranked_votes[1][1])
                ):
                    classification = ranked_votes[0][0]
        if classification is None:
            color = _rgb(item)
            classification = _classify_cycle_color(color, palette, minimum_chroma) if color else None
        if classification is None:
            continue
        classified.append(day_text)
        if classification == "Period":
            actual.append(day_text)
        else:
            known_non_period.append(day_text)
            if classification == "Predicted period":
                predicted.append(day_text)

    metrics = {
        "period_dates": sorted(actual),
        "predicted_period_dates": sorted(predicted),
        "known_non_period_dates": sorted(known_non_period),
        "classified_dates": sorted(classified),
        "classification_complete": len(classified) == days_in_month and len(verified_days) == days_in_month,
        "calendar_coverage": f"{len(verified_days)}/{days_in_month}",
        "calendar_label_coverage": f"{len(seen_days)}/{days_in_month}",
        "calendar_grid_verified": grid_valid,
        "calendar_grid_available": grid_available,
        "calendar_grid_weekdays_match": grid_weekdays_match,
        "calendar_grid_rows_match": grid_rows_match,
        "calendar_grid_sample_shape_match": grid_sample_shape_match,
        "classification_coverage": f"{len(classified)}/{days_in_month}",
        "legend_verified": True,
    }
    return _snapshot("cycle_calendar", observed_at, metrics,
                     period=f"{year:04d}-{month:02d}")


def select_local_fields(text: str) -> list[str]:
    """Select only explicitly named local health pages relevant to the query."""
    query = " ".join(text.casefold().split())
    fields: list[str] = []
    rules = (
        ("cycle_calendar", r"经期|月经|例假|menstrual|predicted period|cycle tracker|\bperiod\b"),
        ("weight_history", r"体重|\bweight\b"),
        ("sun_exposure", r"日照|日晒|sun exposure|sunlight duration"),
        ("wellness_home", r"状态值|状态评分|身心状态|mind and body|wellness"),
        ("active_calories", r"活动卡路里|活动热量|active calories"),
        ("steps_daily_summary", r"步数|\bsteps\b"),
        ("steps_daily_details", r"步行距离|走路距离|步行路程|walking distance|walk distance|\bdistance\b"),
    )
    for field, pattern in rules:
        if re.search(pattern, query, re.IGNORECASE):
            fields.append(field)
            if field == "wellness_home":
                fields.append("wellness_detail")
    return fields


def _snapshot(page: str, observed_at: str | datetime, metrics: dict[str, Any], *,
              selected_date: str | date | None = None, period: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": SOURCE,
        "page": page,
        "observed_at": _observed_at(observed_at),
        "metrics": metrics,
    }
    if selected_date is not None:
        result["date"] = _selected_date(selected_date)
    if period is not None:
        result["period"] = period
    return result


def _require_title(lines: Sequence[str], title: str) -> None:
    if not any(line.casefold() == title.casefold() for line in lines):
        raise ValueError(f"expected visible page label: {title}")


def _weight_value(line: str) -> tuple[int | float, str] | None:
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*(kg|kilograms?|公斤|千克)\s*", line, re.IGNORECASE
    )
    if not match:
        return None
    return _number(match.group(1)), "kg"


def parse_weight_history(
    observations: Iterable[str | Mapping[str, Any]],
    *,
    observed_at: str | datetime,
) -> dict[str, Any]:
    """Keep every visible history row separate; never deduplicate or average."""
    lines = _lines(observations)
    months: list[tuple[int, int]] = []
    current_month: tuple[int, int] | None = None
    pending: tuple[int | float, str, tuple[int, int]] | None = None
    records: list[dict[str, Any]] = []
    time_row = re.compile(r"^\s*(\d{1,2})\s*[,，]\s*(\d{1,2}):([0-5]\d)\s*$")

    for line in lines:
        month = _month_context(line)
        if month:
            current_month = month
            if month not in months:
                months.append(month)
            pending = None
            continue
        value = _weight_value(line)
        if value and current_month:
            pending = (value[0], value[1], current_month)
            continue
        match = time_row.fullmatch(line)
        if match and pending:
            day_num, hour, minute = map(int, match.groups())
            value_num, unit, (year, month_num) = pending
            try:
                record_date = date(year, month_num, day_num)
            except ValueError:
                pending = None
                continue
            records.append({
                "record_date": record_date.isoformat(),
                "measured_at_local": f"{hour:02d}:{minute:02d}",
                "value": value_num,
                "unit": unit,
                "row_index": len(records),
                "observed_at": _observed_at(observed_at),
            })
            pending = None

    metrics = {"weight_history_records": records}
    periods = [f"{year:04d}-{month:02d}" for year, month in months]
    return _snapshot(
        "weight_history", observed_at, metrics,
        period=",".join(periods) if periods else None,
    )



def parse_weight_detail(
    observations: Iterable[str | Mapping[str, Any]],
    *,
    observed_at: str | datetime,
    history_observations: Iterable[str | Mapping[str, Any]] | None = None,
    history_page_status: str | None = None,
    history_observed_at: str | datetime | None = None,
    record_timezone_offset: str | None = None,
    record_timezone_name: str | None = None,
) -> dict[str, Any]:
    """Parse the latest visible weight detail card without substituting capture time."""
    fields: dict[str, str] = {}
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        field = observation.get("field")
        text = observation.get("text")
        if isinstance(field, str) and isinstance(text, str):
            fields[field] = text.strip()

    date_label = fields.get("weight_record_date_label")
    date_parts = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", date_label or "")
    year_match = re.fullmatch(r"20\d{2}", fields.get("weight_record_year", ""))
    record_date = None
    if date_parts and year_match:
        year, month_num, day_num = int(year_match.group(0)), int(date_parts.group(1)), int(date_parts.group(2))
        picker_month = fields.get("weight_record_month", "")
        month_text_match = re.search(r"(\d{1,2})\s*月", picker_month)
        picker_month_num = (
            int(month_text_match.group(1)) if month_text_match
            else _MONTHS.get(picker_month[:3].lower())
        )
        try:
            if picker_month_num is None or picker_month_num == month_num:
                record_date = date(year, month_num, day_num).isoformat()
        except ValueError:
            record_date = None

    time_text = fields.get("weight_record_time", "")
    time_match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", time_text)
    measured_at_local = f"{int(time_match.group(1)):02d}:{time_match.group(2)}" if time_match else None
    value_text = fields.get("weight_record_value", "")
    unit_text = fields.get("weight_record_unit", "")
    parsed_value = _weight_value(f"{value_text} {unit_text}") if value_text and unit_text else None

    history_stamp = history_observed_at or observed_at
    history_result = parse_weight_history(
        history_observations or (), observed_at=history_stamp,
    )
    history_metrics = history_result.get("metrics", {})
    history_records = history_metrics.get("weight_history_records", [])
    if not isinstance(history_records, list):
        history_records = []
    page_status = history_page_status or ("rows_visible" if history_records else "unverified")

    latest_history = None
    if history_records:
        latest_history = max(
            (row for row in history_records if isinstance(row, dict)),
            key=lambda row: (str(row.get("record_date", "")), str(row.get("measured_at_local", ""))),
            default=None,
        )
    latest_matches = None
    if latest_history and record_date and measured_at_local and parsed_value:
        latest_matches = (
            latest_history.get("record_date") == record_date
            and latest_history.get("measured_at_local") == measured_at_local
            and latest_history.get("unit") == parsed_value[1]
            and math.isclose(float(latest_history.get("value", float("nan"))), float(parsed_value[0]), abs_tol=1e-6)
        )
    if page_status == "no_visible_rows":
        history_conflict: bool | None = True
    elif page_status == "rows_visible" and latest_matches is not None:
        history_conflict = not latest_matches
    elif page_status == "rows_visible" and record_date and measured_at_local and parsed_value:
        history_conflict = True
    else:
        history_conflict = None

    records: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {
        "weight_history_records": records,
        "latest_visible_record_status": "unknown" if parsed_value is None else "present",
        "history_page_status": page_status,
        "history_conflict": history_conflict,
        "history_record_count": len(history_records),
        "history_latest_record_matches_detail": latest_matches,
    }
    periods = history_result.get("period")

    if parsed_value is not None:
        value_num, unit = parsed_value
        display_value = value_text.strip().replace(",", ".")
        numeric_value = float(display_value) if "." in display_value else value_num
        recorded_at = None
        timezone_offset = (record_timezone_offset or "").strip()
        normalized_offset = None
        if re.fullmatch(r"[+-]\d{4}", timezone_offset):
            normalized_offset = f"{timezone_offset[:3]}:{timezone_offset[3:]}"
        elif re.fullmatch(r"[+-]\d{2}:\d{2}", timezone_offset):
            normalized_offset = timezone_offset
        if record_date and measured_at_local:
            recorded_at = f"{record_date}T{measured_at_local}:00{normalized_offset or ''}"
        basis = []
        if fields.get("weight_page_title") == "体重":
            basis.append("weight_detail_title_visible")
        if date_label and record_date:
            basis.append("date_header_and_year_picker_visible")
        if measured_at_local:
            basis.append("record_card_time_visible")
        if value_text and unit_text:
            basis.append("weight_value_with_explicit_unit_visible")
        status_label = fields.get("weight_record_status")
        if status_label:
            basis.append("weight_comparison_status_visible")
        if fields.get("weight_record_action") == "记录体重":
            basis.append("separate_record_weight_action_visible")
        if latest_matches is True:
            basis.append("latest_history_row_matches_detail")
        details_complete = all((
            fields.get("weight_page_title") == "体重",
            record_date is not None,
            measured_at_local is not None,
            value_text != "",
            unit_text != "",
            bool(status_label),
            fields.get("weight_record_action") == "记录体重",
        ))
        record: dict[str, Any] = {
            "record_date": record_date,
            "record_date_label": date_label,
            "measured_at_local": measured_at_local,
            "recorded_at": recorded_at,
            "record_timezone": record_timezone_name or None,
            "value": numeric_value,
            "display_value": display_value,
            "unit": unit,
            "source_page": "BodyFatDetailsActivity",
            "record_kind": "latest_visible_record",
            "status_label": status_label,
            "confidence": "high" if details_complete else "medium",
            "confidence_basis": basis,
            "observed_at": _observed_at(observed_at),
            "history_page_status": page_status,
            "history_conflict": history_conflict,
            "history_record_count": len(history_records),
            "history_latest_record_matches_detail": latest_matches,
            "history_observed_at": _observed_at(history_stamp) if history_observations is not None else None,
        }
        records.append(record)

    return _snapshot("weight_history", observed_at, metrics, selected_date=record_date, period=periods)


def parse_sun_exposure(
    observations: Iterable[str | Mapping[str, Any]],
    *,
    selected_date: str | date,
    observed_at: str | datetime,
) -> dict[str, Any]:
    lines = _lines(observations)
    _require_title(lines, "Sun exposure")
    selected = _selected_date(selected_date)
    _visible_day_matches(lines, selected)
    duration = None
    goal = None
    for line in lines:
        if re.search(r"\bgoal\s*:", line, re.IGNORECASE):
            match = re.search(r"(\d+(?:\.\d+)?)\s*(?:min|minutes?|分钟)(?=$|\s)", line, re.IGNORECASE)
            if match:
                goal = _number(match.group(1))
        elif duration is None:
            match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:min|minutes?|分钟)\s*", line, re.IGNORECASE)
            if match:
                duration = _number(match.group(1))
    metrics: dict[str, Any] = {"label": "Sun exposure", "unit": "min"}
    if duration is not None:
        metrics["duration_min"] = duration
    if goal is not None:
        metrics["goal_min"] = goal
    return _snapshot("sun_exposure", observed_at, metrics, selected_date=selected)


def parse_wellness_home(
    observations: Iterable[str | Mapping[str, Any]],
    *,
    selected_date: str | date,
    observed_at: str | datetime,
) -> dict[str, Any]:
    lines = _lines(observations)
    _require_title(lines, "Mind and Body")
    selected = _selected_date(selected_date)
    _visible_day_matches(lines, selected)
    score_line = next((line for line in lines if re.fullmatch(r"\d{1,3}", line)), None)
    value_labels = sorted(_WELLNESS_CATEGORY_LABELS, key=len, reverse=True)
    value_line = next(
        (line for line in lines if any(label.casefold() in line.casefold() for label in value_labels)),
        "",
    )
    value_line_folded = value_line.casefold()
    category_label = next(
        (label for label in value_labels if label.casefold() in value_line_folded),
        None,
    )
    category = next(
        (_WELLNESS_CATEGORY_LABELS[label] for label in (category_label,) if label), None
    )
    paired_score = re.fullmatch(
        r"\s*(\d{1,3})\s*(?:分\s*)?(?:" + "|".join(
            re.escape(label) for label in value_labels
        ) + r")\s*",
        value_line,
        re.IGNORECASE,
    )
    time_match = next((re.fullmatch(r"(\d{1,2}):([0-5]\d)", line) for line in lines
                       if re.fullmatch(r"\d{1,2}:[0-5]\d", line)), None)
    metrics: dict[str, Any] = {
        "label": "Mind and Body",
        "kind": "home_point",
    }
    if paired_score:
        metrics["score"] = int(paired_score.group(1))
    elif score_line:
        metrics["score"] = int(score_line)
    if category:
        metrics["category"] = category
    if time_match:
        metrics["point_time_local"] = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
    return _snapshot("wellness_home", observed_at, metrics, selected_date=selected)


def parse_wellness_detail(
    observations: Iterable[str | Mapping[str, Any]],
    *,
    selected_date: str | date,
    observed_at: str | datetime,
) -> dict[str, Any]:
    lines = _lines(observations)
    _require_title(lines, "Mind and Body")
    if "Avg wellness today" not in lines:
        raise ValueError("expected visible label: Avg wellness today")
    selected = _selected_date(selected_date)
    _visible_day_matches(lines, selected)
    value_line = next((line for line in lines if re.search(r"\bModerate\b|\bExcellent\b|\bGood\b|\bSlow down\b", line)), "")
    match = re.search(r"\b(\d{1,3})\s+(Excellent|Good|Moderate|Slow down)\b", value_line)
    metrics: dict[str, Any] = {
        "label": "Avg wellness today",
        "kind": "daily_average",
        "category_label": "wellness_category",
    }
    if match:
        metrics["score"] = int(match.group(1))
        metrics["category"] = match.group(2)
    return _snapshot("wellness_detail", observed_at, metrics, selected_date=selected)


def parse_active_calories(
    observations: Iterable[str | Mapping[str, Any]],
    *,
    selected_date: str | date,
    observed_at: str | datetime,
) -> dict[str, Any]:
    lines = _lines(observations)
    _require_title(lines, "Active calories")
    selected = _selected_date(selected_date)
    _visible_day_matches(lines, selected)
    title_index = max(i for i, line in enumerate(lines) if line.casefold() == "active calories")
    value = next((int(line) for line in lines[title_index + 1:]
                  if re.fullmatch(r"\d{1,6}", line)), None)
    goal = None
    for line in lines[title_index + 1:]:
        match = re.fullmatch(r"(?:Goal\s*:\s*)?(\d+(?:\.\d+)?)\s*kcal", line, re.IGNORECASE)
        if match:
            goal = _number(match.group(1))
    metrics: dict[str, Any] = {"label": "Active calories", "unit": "kcal"}
    if value is not None:
        metrics["active_kcal"] = value
    if goal is not None:
        metrics["goal_kcal"] = goal
    return _snapshot("active_calories", observed_at, metrics, selected_date=selected)


def _require_day_period(view_period: str) -> None:
    if view_period.casefold() != "day":
        raise ValueError("only the Day view is supported; month/year data is intentionally excluded")


def parse_steps_daily_summary(
    observations: Iterable[str | Mapping[str, Any]],
    *,
    selected_date: str | date,
    observed_at: str | datetime,
    view_period: str = "Day",
) -> dict[str, Any]:
    lines = _lines(observations)
    _require_title(lines, "Steps")
    _require_day_period(view_period)
    selected = _selected_date(selected_date)
    _visible_day_matches(lines, selected)
    title_index = max(i for i, line in enumerate(lines) if line.casefold() == "steps")
    count = next((int(line) for line in lines[title_index + 1:]
                  if re.fullmatch(r"\d{1,6}", line)), None)
    goal = None
    for line in lines[title_index + 1:]:
        match = re.search(r"(?:goal\s*:\s*)?(\d{1,7})\s*steps\b", line, re.IGNORECASE)
        if match:
            goal = int(match.group(1))
            break
    metrics: dict[str, Any] = {"label": "Steps", "unit": "steps"}
    if count is not None:
        metrics["steps"] = count
    if goal is not None:
        metrics["goal_steps"] = goal
    return _snapshot("steps_daily_summary", observed_at, metrics, selected_date=selected)


def parse_steps_daily_details(
    observations: Iterable[str | Mapping[str, Any]],
    *,
    selected_date: str | date,
    observed_at: str | datetime,
    view_period: str = "Day",
) -> dict[str, Any]:
    lines = _lines(observations)
    _require_title(lines, "Steps")
    _require_day_period(view_period)
    selected = _selected_date(selected_date)
    _visible_day_matches(lines, selected)
    distance = None
    for index, line in enumerate(lines[:-1]):
        if line.casefold() == "distance":
            match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(km|kilometers?|公里)\s*", lines[index + 1], re.IGNORECASE)
            if match:
                distance = _number(match.group(1))
            break
    metrics: dict[str, Any] = {"label": "Distance", "unit": "km"}
    if distance is not None:
        metrics["distance_km"] = distance
    return _snapshot("steps_daily_details", observed_at, metrics, selected_date=selected)


def parse_local_page(
    page: str,
    observations: Iterable[str | Mapping[str, Any]],
    *,
    observed_at: str | datetime,
    enabled: bool = LOCAL_ADAPTER_DEFAULT_ENABLED,
    selected_date: str | date | None = None,
    view_period: str = "Day",
    history_observations: Iterable[str | Mapping[str, Any]] | None = None,
    history_page_status: str | None = None,
    history_observed_at: str | datetime | None = None,
    record_timezone_offset: str | None = None,
    record_timezone_name: str | None = None,
) -> dict[str, Any]:
    """Parse one OCR capture only; no merge across pages or capture times."""
    if not enabled:
        raise AdapterDisabled("local health UI adapter is disabled")
    if page == "cycle_calendar":
        return parse_cycle_calendar(observations, observed_at=observed_at)
    if page == "weight_history":
        observations = list(observations)
        if any(
            isinstance(item, Mapping) and item.get("field") in {"weight_page_title", "weight_record_value"}
            for item in observations
        ):
            return parse_weight_detail(
                observations,
                observed_at=observed_at,
                history_observations=history_observations,
                history_page_status=history_page_status,
                history_observed_at=history_observed_at,
                record_timezone_offset=record_timezone_offset,
                record_timezone_name=record_timezone_name,
            )
        return parse_weight_history(observations, observed_at=observed_at)
    if selected_date is None:
        raise ValueError("selected_date is required for this page")
    common = {"selected_date": selected_date, "observed_at": observed_at}
    parsers = {
        "sun_exposure": parse_sun_exposure,
        "wellness_home": parse_wellness_home,
        "wellness_detail": parse_wellness_detail,
        "active_calories": parse_active_calories,
        "steps_daily_summary": parse_steps_daily_summary,
        "steps_daily_details": parse_steps_daily_details,
    }
    try:
        parser = parsers[page]
    except KeyError as exc:
        raise ValueError("unsupported local health page") from exc
    if page.startswith("steps_"):
        return parser(observations, **common, view_period=view_period)
    return parser(observations, **common)
