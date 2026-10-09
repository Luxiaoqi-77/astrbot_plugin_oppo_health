"""Capture requested OPPO Health pages in memory and parse them offline."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Any

from local_health_adapter import parse_local_page


ADB = os.environ.get("OPPO_HEALTH_ADB") or shutil.which("adb")
ADB_SERVER_PORT = os.environ.get("OPPO_HEALTH_ADB_SERVER_PORT")
ADB_SERIAL = os.environ.get("OPPO_HEALTH_EMULATOR_SERIAL", "")
HEALTH_PACKAGE = "com.heytap.health"
SUPPORTED_FIELDS = (
    "cycle_calendar", "weight_history", "sun_exposure", "wellness_home",
    "wellness_detail", "active_calories", "steps_daily_summary",
    "steps_daily_details",
)
_FOREGROUND_CHECKED_AT = 0.0

VISION_SWIFT = r'''import AppKit
import Foundation
import Vision

func rgbSample(_ bitmap: NSBitmapImageRep, x: CGFloat, y: CGFloat) -> [Int]? {
    let centerX = Int((x * CGFloat(bitmap.pixelsWide)).rounded())
    let centerY = Int((y * CGFloat(bitmap.pixelsHigh)).rounded())
    var totals = [CGFloat](repeating: 0, count: 3)
    var count: CGFloat = 0
    for dy in -1...1 {
        for dx in -1...1 {
            let px = centerX + dx
            let py = centerY + dy
            guard px >= 0, py >= 0, px < bitmap.pixelsWide, py < bitmap.pixelsHigh,
                  let color = bitmap.colorAt(x: px, y: py)?.usingColorSpace(.deviceRGB) else {
                continue
            }
            totals[0] += color.redComponent
            totals[1] += color.greenComponent
            totals[2] += color.blueComponent
            count += 1
        }
    }
    guard count > 0 else { return nil }
    return totals.map { Int((($0 / count) * 255).rounded()) }
}

func legendSwatchSample(_ bitmap: NSBitmapImageRep, box: CGRect) -> [Int]? {
    let centerX = Int((box.minX * CGFloat(bitmap.pixelsWide)).rounded())
    let centerY = Int(((1 - box.midY) * CGFloat(bitmap.pixelsHigh)).rounded())
    var bestScore = -1
    var best: [Int]?
    for dy in stride(from: -14, through: 14, by: 2) {
        for dx in stride(from: -32, through: 32, by: 2) {
            let x = CGFloat(centerX + dx) / CGFloat(bitmap.pixelsWide)
            let y = CGFloat(centerY + dy) / CGFloat(bitmap.pixelsHigh)
            guard let color = rgbSample(bitmap, x: x, y: y) else { continue }
            let low = color.min() ?? 0
            let high = color.max() ?? 0
            let brightness = color.reduce(0, +) / color.count
            let score = high - low
            guard brightness > 35, brightness < 250, score > bestScore else { continue }
            bestScore = score
            best = color
        }
    }
    return best
}

let png = FileHandle.standardInput.readDataToEndOfFile()
guard let bitmap = NSBitmapImageRep(data: png), let cgImage = bitmap.cgImage else {
    FileHandle.standardError.write(Data("image_decode_failed".utf8))
    exit(2)
}
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["en-US"]
request.minimumTextHeight = 0.004
let handler = VNImageRequestHandler(cgImage: cgImage)
do {
    try handler.perform([request])
    let observations = (request.results ?? []).compactMap { result -> [String: Any]? in
        let candidates = result.topCandidates(5).map { $0.string }
        guard let fallback = candidates.first else { return nil }
        let cycleLabels = ["Calendar", "Cycle", "Period", "Predicted period",
                           "Fertility window", "Ovulation"]
        let label = candidates.first(where: { candidate in
            let normalized = candidate.trimmingCharacters(in: .whitespacesAndNewlines)
            return cycleLabels.contains {
                $0.caseInsensitiveCompare(normalized) == .orderedSame
            }
        })
        let monthYear = candidates.first(where: { candidate in
            candidate.range(of: #"\b(?:0?[1-9]|1[0-2])\s*[/.-]\s*20\d{2}\b"#,
                            options: .regularExpression) != nil
        })
        let text = label ?? monthYear ?? fallback
        let box = result.boundingBox
        let topY = 1 - box.origin.y - box.height
        // Vision reports normalized boxes from the lower-left. NSBitmapImageRep
        // pixel coordinates use a top-left row origin, so convert before sampling.
        let topMidY = 1 - box.midY
        let dayRGB = rgbSample(bitmap, x: box.midX, y: topMidY - 0.014)
        let legendLabels = ["Period", "Predicted period", "Fertility window", "Ovulation"]
        let isLegendLabel = legendLabels.contains {
            $0.caseInsensitiveCompare(text.trimmingCharacters(in: .whitespacesAndNewlines)) == .orderedSame
        }
        let legendRGB = isLegendLabel
            ? legendSwatchSample(bitmap, box: box)
            : rgbSample(bitmap, x: box.minX - 0.018, y: topMidY)
        var item: [String: Any] = [
            "text": text,
            "bbox": [box.origin.x, topY, box.width, box.height],
        ]
        if let dayRGB { item["sample_rgb"] = dayRGB }
        if let legendRGB { item["legend_rgb"] = legendRGB }
        return item
    }
    let payload: [String: Any] = [
        "width": cgImage.width,
        "height": cgImage.height,
        "observations": observations,
    ]
    let output = try JSONSerialization.data(withJSONObject: payload, options: [])
    FileHandle.standardOutput.write(output)
} catch {
    FileHandle.standardError.write(Data("vision_failed".utf8))
    exit(3)
}
'''


class CaptureError(RuntimeError):
    """Raised when a safe local page capture cannot be completed."""


def _adb(*args: str, timeout: float = 20, input_bytes: bytes | None = None) -> bytes:
    """Run one command against only the existing authorized emulator."""
    if not ADB:
        raise CaptureError("adb_not_configured")
    if not re.fullmatch(r"emulator-\d+", ADB_SERIAL):
        raise CaptureError("emulator_serial_not_configured")
    command = [str(ADB)]
    if ADB_SERVER_PORT:
        if not ADB_SERVER_PORT.isdecimal() or not 1 <= int(ADB_SERVER_PORT) <= 65535:
            raise CaptureError("invalid_adb_server_port")
        command.extend(["-P", ADB_SERVER_PORT])
    command.extend(["-s", ADB_SERIAL, *args])
    process = subprocess.run(
        command,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    if process.returncode != 0:
        raise CaptureError("adb_unavailable")
    return process.stdout


def _foreground_is_health() -> bool:
    activity = _adb("shell", "dumpsys", "activity", "activities", timeout=25)
    return HEALTH_PACKAGE in activity.decode("utf-8", errors="ignore") and bool(
        re.search(r"(?:mResumedActivity|topResumedActivity).*com\.heytap\.health/", activity.decode("utf-8", errors="ignore"))
    )


def _assert_foreground(*, force: bool = False) -> None:
    """Check the expected app at bounded points in a navigation sequence."""
    global _FOREGROUND_CHECKED_AT
    now = time.monotonic()
    if not force and now - _FOREGROUND_CHECKED_AT < 8:
        return
    if not _foreground_is_health():
        raise CaptureError("simulator_not_foreground")
    _FOREGROUND_CHECKED_AT = now


def _compile_vision(temp_dir: Path) -> Path:
    """Compile the built-in macOS Vision reader into a disposable directory."""
    source = temp_dir / "vision.swift"
    executable = temp_dir / "vision"
    # Reuse only Swift framework modules; screenshot and OCR data never enter this cache.
    module_cache = Path(tempfile.gettempdir()) / "oppo-health-vision-module-cache-v1"
    if module_cache.is_symlink():
        raise CaptureError("vision_unavailable")
    module_cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    source.write_text(VISION_SWIFT, encoding="utf-8")
    result = subprocess.run(
        ["swiftc", "-O", "-module-cache-path", str(module_cache),
         str(source), "-o", str(executable)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise CaptureError("vision_unavailable")
    return executable



def _xml_bounds(node: ET.Element) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.attrib.get("bounds", ""))
    return tuple(int(value) for value in match.groups()) if match else None


def _append_ax_field(payload: dict[str, Any], node: ET.Element, field: str,
                     text: str, width: int, height: int) -> None:
    bounds = _xml_bounds(node)
    if bounds is None or width <= 0 or height <= 0:
        return
    left, top, right, bottom = bounds
    if left < 0 or top < 0 or right > width or bottom > height or right <= left or bottom <= top:
        return
    payload["observations"].append({
        "text": text,
        "bbox": [left / width, top / height, (right - left) / width, (bottom - top) / height],
        "source": "accessibility",
        "field": field,
    })


def _append_weight_accessibility_fields(payload: dict[str, Any], root: ET.Element,
                                         width: int, height: int) -> None:
    nodes = list(root.iter())
    text_of = lambda node: (node.attrib.get("text") or node.attrib.get("content-desc") or "").strip()
    texts = [(node, text_of(node)) for node in nodes]

    is_weight_detail = any(text == "体重" for _, text in texts)
    if is_weight_detail:
        title = next(node for node, text in texts if text == "体重")
        _append_ax_field(payload, title, "weight_page_title", "体重", width, height)
        date_pattern = re.compile(r"^\d{1,2}月\d{1,2}日[，,]?\s*周[一二三四五六日天]$")
        for node, text in texts:
            if date_pattern.fullmatch(text):
                _append_ax_field(payload, node, "weight_record_date_label", text, width, height)
                break
        for node, text in texts:
            bounds = _xml_bounds(node)
            if not bounds or not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", text):
                continue
            left, top, right, bottom = bounds
            if top >= int(height * 0.25) and bottom <= int(height * 0.45) and right <= int(width * 0.8):
                _append_ax_field(payload, node, "weight_record_time", text, width, height)
                break
        units = [(node, text) for node, text in texts if text.casefold() in {"公斤", "千克", "kg", "kgs"}]
        numbers = [(node, text) for node, text in texts if re.fullmatch(r"\d+(?:[.,]\d+)?", text)]
        for unit_node, unit_text in units:
            unit_bounds = _xml_bounds(unit_node)
            if unit_bounds is None:
                continue
            unit_left, unit_top, unit_right, unit_bottom = unit_bounds
            unit_mid_y = (unit_top + unit_bottom) / 2
            candidates = []
            for number_node, number_text in numbers:
                number_bounds = _xml_bounds(number_node)
                if number_bounds is None:
                    continue
                number_left, number_top, number_right, number_bottom = number_bounds
                if number_right <= unit_left + 8 and number_top <= unit_mid_y <= number_bottom:
                    candidates.append((unit_left - number_right, number_node, number_text))
            if candidates:
                _, number_node, number_text = min(candidates, key=lambda item: item[0])
                _append_ax_field(payload, number_node, "weight_record_value", number_text.replace(",", "."), width, height)
                _append_ax_field(payload, unit_node, "weight_record_unit", unit_text, width, height)
                break
        for node, text in texts:
            if text.startswith("较上次"):
                _append_ax_field(payload, node, "weight_record_status", text, width, height)
                break
        for node, text in texts:
            if text == "记录体重":
                _append_ax_field(payload, node, "weight_record_action", text, width, height)
                break

    resource_ids = [node.attrib.get("resource-id", "").rsplit("/", 1)[-1] for node in nodes]
    is_date_picker = "cancel" in resource_ids and "month_value" in resource_ids
    if is_date_picker:
        year_node = next((node for node, text in texts
                          if re.fullmatch(r"20\d{2}", text)
                          and (_xml_bounds(node) or (0, 0, 0, 0))[1] < int(height * 0.2)), None)
        month_node = next((node for node in nodes
                           if node.attrib.get("resource-id", "").endswith("/month_value")), None)
        if year_node is not None:
            _append_ax_field(payload, year_node, "weight_record_year", text_of(year_node), width, height)
        if month_node is not None and text_of(month_node):
            _append_ax_field(payload, month_node, "weight_record_month", text_of(month_node), width, height)

    if any(text == "历史体重" for _, text in texts):
        history_observations: list[dict[str, Any]] = []
        month_pattern = re.compile(r"(?:[A-Za-z]{3,9}\s+20\d{2}|20\d{2}\s*年\s*\d{1,2}\s*月|\d{1,2}\s*[/.-]\s*20\d{2})")
        value_pattern = re.compile(r"\d+(?:\.\d+)?\s*(?:公斤|千克|kg|kgs?)", re.IGNORECASE)
        time_pattern = re.compile(r"\d{1,2}\s*[,，]\s*\d{1,2}:[0-5]\d")
        for node, text in texts:
            field = None
            if month_pattern.fullmatch(text):
                field = "weight_history_period"
            elif value_pattern.fullmatch(text):
                field = "weight_history_value"
            elif time_pattern.fullmatch(text):
                field = "weight_history_row_time"
            if field:
                bounds = _xml_bounds(node)
                if bounds is not None:
                    left, top, right, bottom = bounds
                    if 0 <= left < right <= width and 0 <= top < bottom <= height:
                        history_observations.append({
                            "text": " ".join(text.split()),
                            "bbox": [left / width, top / height, (right - left) / width, (bottom - top) / height],
                            "source": "accessibility",
                            "field": field,
                        })
        payload["_weight_history_observations"] = history_observations


def _accessibility_root_live() -> ET.Element | None:
    path = f"/sdcard/oppo-health-capture-{time.monotonic_ns()}.xml"
    try:
        _adb("shell", "uiautomator", "dump", path, timeout=25)
        return ET.fromstring(_adb("shell", "cat", path, timeout=15))
    except (CaptureError, OSError, subprocess.TimeoutExpired, ET.ParseError):
        return None
    finally:
        try:
            _adb("shell", "rm", "-f", path, timeout=10)
        except (CaptureError, OSError, subprocess.TimeoutExpired):
            pass

def _capture(vision: Path) -> dict[str, Any]:
    """Capture local OCR and allowlisted accessibility labels in memory."""
    png = _adb("exec-out", "screencap", "-p", timeout=25)
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CaptureError("screenshot_unavailable")
    result = subprocess.run(
        [str(vision)], input=png, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False, timeout=25,
    )
    del png
    if result.returncode != 0:
        raise CaptureError("vision_unavailable")
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CaptureError("vision_unavailable") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("observations"), list):
        raise CaptureError("vision_unavailable")

    width = int(payload.get("width", 0))
    height = int(payload.get("height", 0))
    accessible_root = _accessibility_root_live()

    # Fill OCR-missed supported page labels from accessibility; Vision supplies dates and values.
    page_labels = {
        "calendar", "cycle", "period", "predicted period", "fertility window",
        "ovulation", "cycle tracker", "period tracker", "menstrual cycle",
        "weight", "body weight", "weight history", "history", "history records",
        "sun exposure", "mind and body", "avg wellness today", "active calories",
        "calories", "steps", "distance", "daily activity", "health", "all data",
        "health data", "health records", "see all", "view all",
        "体重", "历史体重", "待认领数据", "较上次持平", "记录体重", "公斤", "cancel", "取消",
    }
    if accessible_root is not None and width > 0 and height > 0:
        existing_texts = {
            item["text"].casefold()
            for item in payload["observations"]
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        }
        for node in accessible_root.iter():
            label = (
                node.attrib.get("text")
                or node.attrib.get("content-desc")
                or ""
            ).strip()
            if not label or label.casefold() not in page_labels or label.casefold() in existing_texts:
                continue
            bounds = re.fullmatch(
                r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                node.attrib.get("bounds", ""),
            )
            if not bounds:
                continue
            left, top, right, bottom = (int(value) for value in bounds.groups())
            if (left < 0 or top < 0 or right > width or bottom > height
                    or right <= left or bottom <= top):
                continue
            payload["observations"].append({
                "text": label,
                "bbox": [
                    left / width, top / height,
                    (right - left) / width, (bottom - top) / height,
                ],
                "source": "accessibility",
            })
            existing_texts.add(label.casefold())
        _append_weight_accessibility_fields(payload, accessible_root, width, height)
    return payload


def _tap_weight_overflow(screen: dict[str, Any]) -> bool:
    root = _accessibility_root_live()
    if root is None:
        return False
    width, height = int(screen.get("width", 0)), int(screen.get("height", 0))
    candidates = []
    for node in root.iter():
        bounds = _xml_bounds(node)
        if bounds is None or node.attrib.get("clickable", "").casefold() != "true":
            continue
        left, top, right, bottom = bounds
        if left >= int(width * 0.83) and top <= int(height * 0.18):
            candidates.append((right, bounds))
    if not candidates:
        return False
    _, (left, top, right, bottom) = max(candidates, key=lambda item: item[0])
    _assert_foreground()
    _adb("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))
    time.sleep(0.45)
    return True


def _weight_history_page_status(screen: dict[str, Any]) -> str:
    if not _page(screen, "历史体重"):
        return "unavailable"
    rows = screen.get("_weight_history_observations", [])
    periods = sum(item.get("field") == "weight_history_period" for item in rows if isinstance(item, dict))
    values = sum(item.get("field") == "weight_history_value" for item in rows if isinstance(item, dict))
    times = sum(item.get("field") == "weight_history_row_time" for item in rows if isinstance(item, dict))
    return "rows_visible" if periods and values and times else "no_visible_rows"


def _emulator_timezone_context() -> tuple[str | None, str | None]:
    try:
        offset = _adb("shell", "date", "+%z").decode("ascii", errors="ignore").strip()
        zone = _adb("shell", "getprop", "persist.sys.timezone").decode("utf-8", errors="ignore").strip()
    except (CaptureError, OSError, subprocess.TimeoutExpired):
        return None, None
    return (offset or None, zone or None)


def _texts(screen: dict[str, Any]) -> list[str]:
    return [item.get("text", "") for item in screen.get("observations", [])
            if isinstance(item, dict) and isinstance(item.get("text"), str)]


def _label(screen: dict[str, Any], candidates: tuple[str, ...]) -> dict[str, Any] | None:
    wanted = {candidate.casefold() for candidate in candidates}
    matches = [item for item in screen.get("observations", [])
               if isinstance(item, dict) and isinstance(item.get("text"), str)
               and item["text"].strip().casefold() in wanted
               and isinstance(item.get("bbox"), list) and len(item["bbox"]) == 4]
    if not matches:
        return None
    return max(matches, key=lambda item: item["bbox"][1])


def _tap_observation(screen: dict[str, Any], observation: dict[str, Any]) -> None:
    _assert_foreground()
    # VISION_SWIFT already converts Vision's lower-left box origin to top-left.
    x, y, width, height = (float(value) for value in observation["bbox"])
    screen_width = int(screen["width"])
    screen_height = int(screen["height"])
    px = max(1, min(screen_width - 1, int((x + width / 2) * screen_width)))
    py = max(1, min(screen_height - 1, int((y + height / 2) * screen_height)))
    _adb("shell", "input", "tap", str(px), str(py))
    time.sleep(0.55)


def _tap_label(screen: dict[str, Any], candidates: tuple[str, ...]) -> dict[str, Any] | None:
    found = _label(screen, candidates)
    if found is None:
        return None
    _tap_observation(screen, found)
    return found


def _back() -> None:
    _assert_foreground()
    _adb("shell", "input", "keyevent", "4")
    time.sleep(0.4)


def _health_home_from_accessibility(raw_xml: bytes) -> bool | None:
    """Identify the Health dashboard without relying on its possibly offscreen title."""
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return None
    nodes = list(root.iter())
    health_tab_selected = any(
        "app_navigation_health" in node.attrib.get("resource-id", "")
        and node.attrib.get("selected", "").casefold() == "true"
        for node in nodes
    )
    management_grid = any(
        "home_card_list" in node.attrib.get("resource-id", "") for node in nodes
    )
    detail_back = any(
        "toolbar_back_view" in node.attrib.get("resource-id", "").casefold()
        or "back" in node.attrib.get("content-desc", "").casefold()
        for node in nodes
    )
    return health_tab_selected and not management_grid and not detail_back


def _health_home_from_accessibility_live() -> bool | None:
    path = "/sdcard/window.xml"
    try:
        _adb("shell", "uiautomator", "dump", path, timeout=25)
        return _health_home_from_accessibility(_adb("shell", "cat", path, timeout=15))
    except (CaptureError, OSError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            _adb("shell", "rm", "-f", path, timeout=10)
        except (CaptureError, OSError, subprocess.TimeoutExpired):
            pass


def _is_home(screen: dict[str, Any]) -> bool:
    cached = screen.get("_health_home_cache")
    if isinstance(cached, bool):
        return cached
    if _foreground_is_health():
        accessible_state = _health_home_from_accessibility_live()
        if accessible_state is not None:
            screen["_health_home_cache"] = accessible_state
            return accessible_state
    texts = {text.casefold() for text in _texts(screen)}
    if "health" not in texts:
        return False
    markers = {"steps", "weight", "sun exposure", "mind and body",
               "active calories", "cycle tracker", "sleep"}
    result = len(texts & markers) >= 1
    screen["_health_home_cache"] = result
    return result


def _page(screen: dict[str, Any], expected: str) -> bool:
    return expected.casefold() in {text.strip().casefold() for text in _texts(screen)}


def _has_visible_day(screen: dict[str, Any]) -> bool:
    for line in _texts(screen):
        if re.search(r"\btoday\b", line, re.IGNORECASE):
            return True
        if re.search(r"\b[A-Za-z]{3,9}\s+\d{1,2}(?:,|\s)", line):
            return True
        if re.search(r"\d{1,2}\s*月\s*\d{1,2}\s*日", line):
            return True
    return False


def _page_has_detail_context(page: str, screen: dict[str, Any], title: str) -> bool:
    """Require page-specific context so a summary-card title is not a detail page."""
    if not _page(screen, title):
        return False
    lines = [text.strip() for text in _texts(screen) if text.strip()]
    texts = {text.casefold() for text in lines}
    joined = "\n".join(lines)
    if page == "cycle_calendar":
        return _is_cycle_calendar(screen)
    if page == "weight_history":
        return bool(texts & {"history", "weight history", "history records"})
    if not _has_visible_day(screen):
        return False
    if page == "sun_exposure":
        return bool(re.search(
            r"\d+(?:\.\d+)?\s*(?:min|minutes?)\b|\d+(?:\.\d+)?\s*分钟",
            joined, re.IGNORECASE,
        ))
    if page == "wellness_home":
        score = any(re.fullmatch(r"\d{1,3}", line) for line in lines)
        category = bool(texts & {"excellent", "good", "moderate", "slow down"})
        point_time = any(re.fullmatch(r"\d{1,2}:[0-5]\d", line) for line in lines)
        return score and (category or point_time)
    if page == "wellness_detail":
        score = any(re.fullmatch(r"\d{1,3}", line) for line in lines)
        category = bool(re.search(r"\b(?:Excellent|Good|Moderate|Slow down)\b", joined, re.IGNORECASE))
        return "avg wellness today" in texts and (score or category)
    if page == "active_calories":
        return bool(re.search(r"\d+(?:\.\d+)?\s*kcal\b", joined, re.IGNORECASE))
    if page == "steps_daily_summary":
        return "day" in texts and bool(re.search(r"\d+\s*steps\b", joined, re.IGNORECASE))
    if page == "steps_daily_details":
        return "distance" in texts and bool(re.search(r"\d+(?:\.\d+)?\s*(?:km|kilometers?|公里)\b", joined, re.IGNORECASE))
    return False


def _is_cycle_calendar(screen: dict[str, Any]) -> bool:
    texts = {text.strip().casefold() for text in _texts(screen)}
    return (
        "calendar" in texts
        and "cycle" in texts
        and bool(texts & {"period", "predicted period"})
    )


def _go_home(screen: dict[str, Any], vision: Path, *, max_back: int = 1) -> dict[str, Any] | None:
    if _is_home(screen):
        return screen
    _assert_foreground()
    # Prefer a visible Health tab, then a bounded number of in-app Back actions.
    if _tap_label(screen, ("Health",)) is not None:
        next_screen = _capture(vision)
        if _is_home(next_screen):
            return next_screen
    for _ in range(max_back):
        _back()
        _assert_foreground(force=True)
        next_screen = _capture(vision)
        if _is_home(next_screen):
            return next_screen
        screen = next_screen
    return None


def _emulator_date() -> str:
    value = _adb("shell", "date", "+%Y-%m-%d").decode("ascii", errors="ignore").strip()
    return dt.date.fromisoformat(value).isoformat()


def _find_tile(screen: dict[str, Any], vision: Path, candidates: tuple[str, ...]) -> dict[str, Any] | None:
    current = screen
    for _ in range(5):
        if _tap_label(current, candidates) is not None:
            return _capture(vision)
        width, height = int(current["width"]), int(current["height"])
        _adb("shell", "input", "swipe", str(width // 2), str(int(height * 0.78)),
             str(width // 2), str(int(height * 0.38)), "350")
        time.sleep(0.35)
        _assert_foreground()
        current = _capture(vision)
    for _ in range(5):
        _assert_foreground()
        width, height = int(current["width"]), int(current["height"])
        _adb("shell", "input", "swipe", str(width // 2), str(int(height * 0.38)),
             str(width // 2), str(int(height * 0.78)), "350")
        time.sleep(0.3)
    return None


def _parse(page: str, screen: dict[str, Any], *, observed_at: str, selected_date: str | None = None) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if page == "weight_history":
        extra = {
            "history_observations": screen.get("_weight_history_observations"),
            "history_page_status": screen.get("_weight_history_page_status"),
            "history_observed_at": screen.get("_weight_history_observed_at"),
            "record_timezone_offset": screen.get("_weight_record_timezone_offset"),
            "record_timezone_name": screen.get("_weight_record_timezone_name"),
        }
    return parse_local_page(
        page,
        screen["observations"],
        observed_at=observed_at,
        enabled=True,
        selected_date=selected_date,
        view_period="Day",
        **extra,
    )


def _capture_field(field: str, screen: dict[str, Any], vision: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Navigate only through visible labels and capture one requested page."""
    home = _go_home(screen, vision, max_back=2 if field == "weight_history" else 1)
    if home is None:
        return None, None
    routes = {
        "cycle_calendar": (("Cycle Tracker", "Period tracker", "Menstrual cycle"), "Cycle Tracker"),
        "weight_history": (("Weight", "Body weight"), "体重"),
        "sun_exposure": (("Sun exposure",), "Sun exposure"),
        "wellness_home": (("Mind and Body",), "Mind and Body"),
        "wellness_detail": (("Mind and Body",), "Mind and Body"),
        "active_calories": (("Active calories", "Calories"), "Active calories"),
        "steps_daily_summary": (("Steps",), "Steps"),
        "steps_daily_details": (("Steps",), "Steps"),
    }
    labels, title = routes[field]
    target = _find_tile(home, vision, labels)
    if target is None:
        return None, home
    if field == "cycle_calendar":
        if not _is_cycle_calendar(target):
            return None, _go_home(target, vision)
    elif field == "weight_history":
        if not (_page(target, "体重") or _page(target, "Weight")):
            return None, _go_home(target, vision)
    elif not _page(target, title):
        return None, _go_home(target, vision)

    if field == "cycle_calendar":
        if _tap_label(target, ("Calendar",)) is None:
            return None, _go_home(target, vision)
        target = _capture(vision)
        if not _is_cycle_calendar(target):
            return None, _go_home(target, vision)
    elif field == "weight_history":
        date_observation = next((
            item for item in target.get("observations", [])
            if isinstance(item, dict) and item.get("field") == "weight_record_date_label"
        ), None)
        if date_observation is not None:
            _tap_observation(target, date_observation)
            date_picker = _capture(vision)
            date_metadata = [
                item for item in date_picker.get("observations", [])
                if isinstance(item, dict) and item.get("field") in {"weight_record_year", "weight_record_month"}
            ]
            cancel = _label(date_picker, ("Cancel", "取消"))
            if cancel is not None:
                _tap_observation(date_picker, cancel)
            else:
                _back()
            time.sleep(0.35)
            refreshed = _capture(vision)
            if _page(refreshed, "体重") or _page(refreshed, "Weight"):
                target = refreshed
            target.setdefault("observations", []).extend(date_metadata)

        target["_weight_record_observed_at"] = dt.datetime.now().astimezone().isoformat()
        offset, zone = _emulator_timezone_context()
        target["_weight_record_timezone_offset"] = offset
        target["_weight_record_timezone_name"] = zone

        history_status = "unavailable"
        history_screen = None
        if _tap_weight_overflow(target):
            menu_screen = _capture(vision)
            if _tap_label(menu_screen, ("历史体重", "History Weight", "Weight history", "History")) is not None:
                for attempt in range(4):
                    if attempt:
                        time.sleep(0.65)
                    history_screen = _capture(vision)
                    history_status = _weight_history_page_status(history_screen)
                    if history_status == "rows_visible":
                        break
        if history_screen is not None:
            target["_weight_history_observations"] = history_screen.get("_weight_history_observations", [])
            target["_weight_history_page_status"] = history_status
            target["_weight_history_observed_at"] = dt.datetime.now().astimezone().isoformat()
        else:
            target["_weight_history_observations"] = []
            target["_weight_history_page_status"] = "unavailable"
            target["_weight_history_observed_at"] = None
    elif field == "wellness_detail":
        if _tap_label(target, ("Avg wellness today",)) is not None:
            target = _capture(vision)
    elif field.startswith("steps_"):
        if _label(target, ("Day",)) is not None:
            _tap_label(target, ("Day",))
            target = _capture(vision)
        elif any(text.casefold() in {"month", "year"} for text in _texts(target)):
            return None, _go_home(target, vision)
        if field == "steps_daily_details" and not _page(target, "Distance"):
            if _label(target, ("Distance",)) is None:
                width, height = int(target["width"]), int(target["height"])
                for _ in range(4):
                    _adb("shell", "input", "swipe", str(width // 2),
                         str(int(height * 0.88)), str(width // 2),
                         str(int(height * 0.35)), "400")
                    time.sleep(0.35)
                    _assert_foreground()
                    target = _capture(vision)
                    if _label(target, ("Distance",)) is not None:
                        break
            if _tap_label(target, ("Distance",)) is not None:
                target = _capture(vision)

    if field == "cycle_calendar":
        page_verified = _is_cycle_calendar(target)
    elif field == "weight_history":
        page_verified = _page(target, "体重") and any(
            isinstance(item, dict) and item.get("field") == "weight_page_title"
            for item in target.get("observations", [])
        )
    else:
        page_verified = _page_has_detail_context(field, target, title)
    if not page_verified:
        return None, _go_home(target, vision, max_back=2 if field in ("weight_history", "steps_daily_details") else 1)
    home = _go_home(
        target,
        vision,
        max_back=2 if field in ("weight_history", "steps_daily_details") else 1,
    )
    return target, home


def _status(page: str, data: dict[str, Any]) -> str:
    metrics = data.get("metrics", {})
    if page == "cycle_calendar":
        return "ok" if metrics.get("legend_verified") and metrics.get("calendar_coverage") else "page_unverified"
    fields = {
        "cycle_calendar": ("period_dates", "predicted_period_dates"),
        "weight_history": ("weight_history_records",),
        "sun_exposure": ("duration_min", "goal_min"),
        "wellness_home": ("score", "category", "point_time_local"),
        "wellness_detail": ("score", "category"),
        "active_calories": ("active_kcal", "goal_kcal"),
        "steps_daily_summary": ("steps", "goal_steps"),
        "steps_daily_details": ("distance_km",),
    }
    return "ok" if any(key in metrics and metrics[key] not in (None, []) for key in fields[page]) else "no_visible_value"


def collect(fields: list[str], *, labels_only: bool = False) -> dict[str, Any]:
    """Capture only selected pages from the already-open health simulator.

    Args:
        fields: Explicit page identifiers selected for this request.
        labels_only: Return alphabetic labels only for local navigation review.

    Returns:
        A page-scoped result with capture timestamps and no raw screenshot data.
    """
    if not fields or any(field not in SUPPORTED_FIELDS for field in fields):
        return {"status": "unavailable", "reason": "unsupported_request", "pages": {}}
    try:
        if not ADB or not Path(ADB).is_file() or not re.fullmatch(r"emulator-\d+", ADB_SERIAL):
            return {"status": "unavailable", "reason": "local_capture_not_configured", "pages": {}}
        _assert_foreground(force=True)
    except (CaptureError, OSError, subprocess.TimeoutExpired):
        return {"status": "unavailable", "reason": "adb_unavailable", "pages": {}}

    pages: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="oppo-health-local-") as temp_name:
            vision = _compile_vision(Path(temp_name))
            screen = _capture(vision)
            if labels_only:
                labels = [text for text in _texts(screen)
                          if re.fullmatch(r"[A-Za-z][A-Za-z &'-]*", text.strip())]
                return {"status": "ok", "reason": None, "labels": labels}

            started_on_cycle_page = _is_cycle_calendar(screen)
            if not _is_home(screen) and started_on_cycle_page and "cycle_calendar" in fields:
                if _page(screen, "Calendar") and _page(screen, "Cycle"):
                    stamp = dt.datetime.now().astimezone().isoformat()
                    try:
                        data = _parse("cycle_calendar", screen, observed_at=stamp)
                        pages["cycle_calendar"] = {"status": _status("cycle_calendar", data), "data": data}
                    except (ValueError, TypeError) as exc:
                        pages["cycle_calendar"] = {
                            "status": "page_unverified",
                            "reason": str(exc),
                        }
                    fields = [field for field in fields if field != "cycle_calendar"]

            if not fields:
                status = "ok" if any(value.get("data") for value in pages.values()) else "unavailable"
                return {"status": status, "reason": None if status == "ok" else "no_page_verified", "pages": pages}
            home = _go_home(screen, vision, max_back=2 if "weight_history" in fields else 1)
            if home is None and fields:
                return {"status": "unavailable", "reason": "health_home_unavailable", "pages": pages}
            current = home or screen
            date_context = _emulator_date()
            for field in fields:
                page_screen, home = _capture_field(field, current, vision)
                if page_screen is None:
                    pages[field] = {"status": "page_unavailable", "reason": "navigation_unverified"}
                    current = home or current
                    if home is None:
                        break
                    continue
                stamp = page_screen.get("_weight_record_observed_at") if field == "weight_history" else None
                stamp = stamp or dt.datetime.now().astimezone().isoformat()
                selected_date = None if field in ("cycle_calendar", "weight_history") else date_context
                try:
                    data = _parse(field, page_screen, observed_at=stamp, selected_date=selected_date)
                    pages[field] = {"status": _status(field, data), "data": data}
                except (ValueError, TypeError) as exc:
                    pages[field] = {"status": "page_unverified", "reason": str(exc)}
                current = home or current
                if home is None:
                    break
            if started_on_cycle_page and current and _is_home(current):
                restored = _find_tile(
                    current, vision, ("Cycle Tracker", "Period tracker", "Menstrual cycle")
                )
                if restored and _is_cycle_calendar(restored):
                    if _tap_label(restored, ("Calendar",)) is not None:
                        _capture(vision)
            status = "ok" if any(value.get("data") for value in pages.values()) else "unavailable"
            return {"status": status, "reason": None if status == "ok" else "no_page_verified", "pages": pages}
    except (CaptureError, OSError, subprocess.TimeoutExpired):
        return {"status": "unavailable", "reason": "local_capture_unavailable", "pages": pages}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read explicitly requested OPPO Health pages locally.")
    parser.add_argument("--fields", required=True, help="Comma-separated supported page identifiers")
    parser.add_argument("--json", action="store_true", help="Write structured local results")
    parser.add_argument("--labels-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    fields = list(dict.fromkeys(field.strip() for field in args.fields.split(",") if field.strip()))
    result = collect(fields, labels_only=args.labels_only)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
