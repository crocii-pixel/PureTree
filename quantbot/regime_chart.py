"""Native Qt BTC chart used by the non-modal backtest window.

The widget deliberately avoids QtWebEngine: existing QuantBot deployments do
not bundle Chromium or PyQtWebEngine.  It provides logarithmic price scale,
zoom/pan/crosshair, transparent regime regions, detector ribbons and log-price
MACD while sharing the exact detector settings used by the backtest.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
import threading

import numpy as np
import pandas as pd

from regime_scoring import (REGIME_COLORS, build_regime_frame,
                            current_regime_decision, scoring_config,
                            validate_scoring_config)


CHART_INTERVALS: Tuple[Tuple[str, str], ...] = (
    ("1m", "1m"), ("15m", "15m"), ("30m", "30m"),
    ("1h", "1h"), ("2h", "2h"), ("3h", "3h"), ("4h", "4h"),
    ("1d", "D"), ("1w", "W"), ("1mo", "M"),
)

CHART_INTERVAL_SECONDS: Dict[str, int] = {
    "1m": 60, "15m": 900, "30m": 1_800,
    "1h": 3_600, "2h": 7_200, "3h": 10_800, "4h": 14_400,
    "1d": 86_400, "1w": 604_800, "1mo": 2_629_800,
}

FULL_PERIOD_INTERVALS = {"1d", "1w", "1mo"}

MIN_VISIBLE_BARS: Dict[str, int] = {
    "1m": 120, "15m": 80, "30m": 80,
    "1h": 72, "2h": 60, "3h": 56, "4h": 48,
    "1d": 30, "1w": 12, "1mo": 6,
}

#: 차트 위에 겹쳐 그리는 보조선. (열 이름, 라벨, 색, 선 모양키)
#: 순서가 곧 범례 순서이고, 범례를 클릭하면 그 선만 껐다 켤 수 있습니다.
OVERLAY_SERIES: Tuple[Tuple[str, str, str, str], ...] = (
    ("ma_short", "단기 MA", "#22D3EE", "solid"),
    ("ma_long", "장기 MA", "#F59E0B", "dash"),
    ("buy_target", "매수기준", "#3B82F6", "dot"),
    ("atr_upper_level", "ATR 상단", "#60A5FA", "dash"),
    ("atr_lower_level", "ATR 하단", "#FB7185", "dash"),
    ("lower_channel_line", "하방채널", "#EF4444", "solid"),
)

#: 판정값 입력란 공통 폭. 기존 76px 의 2/3.
PARAM_INPUT_WIDTH = 50


def compact_spin(widget: Any, target: int = PARAM_INPUT_WIDTH) -> Any:
    """
    숫자 입력란을 ``target`` 폭으로 좁힙니다.

    폰트가 큰 환경에서 값이 잘리면 설정을 확인할 수 없으므로, 최댓값 글자가
    안 들어가는 경우에만 필요한 만큼 늘립니다.  ``compact`` 속성은 여백과
    화살표를 줄이는 스타일시트 규칙을 켭니다.
    """
    widget.setProperty("compact", "true")
    text = widget.textFromValue(widget.maximum())
    if getattr(widget, "suffix", None):
        text += widget.suffix()
    # 테두리 2 + 좌우 여백 7 + 화살표 11 + 커서 여유 2
    needed = widget.fontMetrics().horizontalAdvance(text) + 22
    widget.setFixedWidth(max(int(target), int(needed)))
    return widget
#: 콤보 폭을 맞출 때 넘지 않을 상한. 패널이 통째로 넓어지는 것을 막습니다.
#: 가장 긴 항목(장세별 전략 콤보)이 잘리지 않는 선에서 잡았습니다.
FIELD_WIDTH_CAP = 250

DEFAULT_VISIBLE_BARS: Dict[str, int] = {
    "1m": 360, "15m": 240, "30m": 240,
    "1h": 240, "2h": 180, "3h": 160, "4h": 150,
    "1d": 180, "1w": 104, "1mo": 48,
}

DETECTOR_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("dual_ma", "이중 이동평균 추세"),
    ("log_macd", "로그 MACD"),
    ("volatility_breakout", "ATR 변동성 돌파"),
    ("volatility_decline", "ATR 변동성 하락"),
    ("lower_channel", "하방 채널선"),
)
BULL_DETECTOR_OPTIONS = tuple(
    item for item in DETECTOR_OPTIONS if item[0] != "volatility_decline")
BEAR_DETECTOR_OPTIONS = tuple(
    item for item in DETECTOR_OPTIONS if item[0] != "volatility_breakout")

#: 예약매수 기준선은 이제 '예약매수 방식'에서 ATR 하단과 하방 채널선 중에
#: 고릅니다. 전략 이름에 "ATR 하단"을 박아 두면 틀린 설명이 되고, 콤보가
#: 패널 폭을 넘길 만큼 길어집니다.
STRATEGY_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("period_rebalance", "주간 기간리밸런싱"),
    ("volatility_breakout", "동적 K 변동성돌파"),
    ("defensive_atr", "동적 K + 예약매수"),
    ("cash_with_atr", "현금 대기 + 예약매수"),
    ("cash", "현금 대기"),
)


def minimum_visible_span(interval: str) -> pd.Timedelta:
    return pd.Timedelta(
        seconds=CHART_INTERVAL_SECONDS[interval] * MIN_VISIBLE_BARS[interval])


def aggregate_chart_frame(frame: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Build closed week/month bars from the canonical daily archive."""
    if interval not in {"1w", "1mo"} or frame.empty:
        return frame.copy()
    source = frame.copy()
    if "timestamp" in source.columns:
        index = pd.DatetimeIndex(pd.to_datetime(
            source.pop("timestamp"), utc=True, errors="coerce"))
        source.index = index
    elif not isinstance(source.index, pd.DatetimeIndex):
        source.index = pd.DatetimeIndex(pd.to_datetime(
            source.index, utc=True, errors="coerce"))
    source = source[~source.index.isna()].sort_index()
    rule = "W-MON" if interval == "1w" else "MS"
    result = source.resample(rule, label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum",
    }).dropna(subset=["open", "close"])
    if not result.empty:
        first_source = _timestamp(source.index[0])
        last_source = _timestamp(source.index[-1])
        first_bucket = _timestamp(result.index[0])
        last_bucket = _timestamp(result.index[-1])
        first_complete = (first_source.normalize() == first_bucket.normalize())
        if interval == "1w":
            last_required = last_bucket + pd.Timedelta(days=6)
        else:
            last_required = last_bucket + pd.offsets.MonthEnd(0)
        last_complete = last_source.normalize() >= _timestamp(last_required).normalize()
        if not first_complete:
            result = result.iloc[1:]
        if not result.empty and not last_complete:
            result = result.iloc[:-1]
    return result.reset_index(names="timestamp")


def contiguous_regions(labels: pd.Series) -> List[Dict[str, Any]]:
    """Compress an ordered label series while respecting real data gaps."""
    labels = labels.dropna()
    if labels.empty:
        return []
    index = pd.DatetimeIndex(labels.index)
    deltas = index.to_series().diff().dropna()
    typical = deltas.median() if not deltas.empty else pd.Timedelta(days=1)
    result: List[Dict[str, Any]] = []
    start = previous = labels.index[0]
    current = str(labels.iloc[0])
    for stamp, value in labels.iloc[1:].items():
        value = str(value)
        gap = pd.Timestamp(stamp) - pd.Timestamp(previous)
        if value != current or gap > typical * 1.5:
            result.append({"start": pd.Timestamp(start), "end": pd.Timestamp(previous),
                           "label": current})
            start, current = stamp, value
        previous = stamp
    result.append({"start": pd.Timestamp(start), "end": pd.Timestamp(previous),
                   "label": current})
    return result


def _event_pos(event: Any) -> Any:
    return event.position() if hasattr(event, "position") else event.localPos()


def _timestamp(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("UTC").tz_localize(None)
    return stamp


def _period_bounds(start: Any, end: Any) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """Return an inclusive civil-date range for chart display/analysis."""
    start_ts, end_ts = _timestamp(start), _timestamp(end)
    if end_ts == end_ts.normalize():
        end_ts += pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return start_ts, end_ts


def _finite(value: Any) -> bool:
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def build_regime_chart_window(QtCore: Any, QtGui: Any, QtWidgets: Any,
                              config_provider: Callable[[], Dict[str, Any]],
                              config_changed: Callable[[Dict[str, Any]], None],
                              period_provider: Callable[[], Tuple[Any, Any, bool]],
                              period_apply: Callable[[pd.Timestamp, pd.Timestamp], None],
                              parent: Any = None) -> Any:
    """Build a Qt-version-compatible non-modal chart window instance."""

    class PriceChart(QtWidgets.QWidget):
        viewChanged = QtCore.pyqtSignal(object, object)
        windowRequested = QtCore.pyqtSignal(object, object)

        def __init__(self):
            super().__init__()
            self.setMinimumSize(680, 500)
            self.setMouseTracking(True)
            self._data = pd.DataFrame()
            self._diagnostic = pd.DataFrame()
            self._regions: List[Dict[str, Any]] = []
            self._log_scale = True
            #: 보조선별 표시 여부. 차트 좌상단 범례를 눌러 바꿉니다.
            self._series_visible: Dict[str, bool] = {
                column: True for column, _l, _c, _s in OVERLAY_SERIES}
            self._legend_hit: Dict[str, Any] = {}
            self._show_regime = True
            self._show_macd = True
            self._show_conditions = True
            self._view_start: Optional[pd.Timestamp] = None
            self._view_end: Optional[pd.Timestamp] = None
            self._period_start: Optional[pd.Timestamp] = None
            self._period_end: Optional[pd.Timestamp] = None
            self._drag_origin = None
            self._drag_range = None
            self._drag_last_point = None
            self._drag_preview_dx = 0.0
            self._crosshair = None
            self._interval = "1d"
            self._interval_label = "D"
            self._bar_seconds = CHART_INTERVAL_SECONDS["1d"]
            self._focus_time: Optional[pd.Timestamp] = None
            self._focus_ratio = 0.5
            self._data_index = pd.DatetimeIndex([])
            self._diagnostic_index = pd.DatetimeIndex([])
            self._static_pixmap = None
            self._static_key = None
            self._macd_fraction = 0.24
            self._splitter_dragging = False
            self._edge_feedback: Optional[str] = None

        def set_interval(self, interval: str) -> None:
            labels = dict(CHART_INTERVALS)
            self._interval = interval
            self._interval_label = labels.get(interval, interval)
            self._bar_seconds = CHART_INTERVAL_SECONDS.get(interval, 86_400)

        def _minimum_span(self) -> pd.Timedelta:
            return minimum_visible_span(self._interval)

        def anchor_state(self) -> Optional[Dict[str, Any]]:
            if (self._data.empty or self._view_start is None
                    or self._view_end is None):
                return None
            price_rect, _, _ = self._layout()
            ratio = float(np.clip(self._focus_ratio, 0.0, 1.0))
            anchor = self._focus_time
            if anchor is None or not (self._view_start <= anchor <= self._view_end):
                anchor = self._time_at(
                    price_rect.left() + ratio * price_rect.width(), price_rect)
            return {
                "time": anchor,
                "ratio": ratio,
                "span": self._view_end - self._view_start,
                "start": self._view_start,
                "end": self._view_end,
            }

        def restore_anchor(self, anchor: Any, ratio: float,
                           span: pd.Timedelta) -> None:
            if self._data.empty:
                return
            anchor_ts = _timestamp(anchor)
            ratio = float(np.clip(ratio, 0.0, 1.0))
            start = anchor_ts - span * ratio
            end = start + span
            self._view_start, self._view_end = self._clamp_view(start, end)
            self._focus_time, self._focus_ratio = anchor_ts, ratio
            self.viewChanged.emit(self._view_start, self._view_end)
            self.update()

        def set_frames(self, data: pd.DataFrame, diagnostic: pd.DataFrame) -> None:
            self._data = data.copy()
            self._diagnostic = diagnostic.copy()
            self._data_index = pd.DatetimeIndex(
                [_timestamp(value) for value in self._data.index])
            self._diagnostic_index = pd.DatetimeIndex(
                [_timestamp(value) for value in self._diagnostic.index])
            # Pandas 3 may preserve repository indexes as datetime64[us].
            # Dragging produces arbitrary nanosecond timestamps; searchsorted
            # rejects that comparison as a lossy conversion unless both sides
            # use the same high-resolution unit.
            if hasattr(self._data_index, "as_unit"):
                self._data_index = self._data_index.as_unit("ns")
                self._diagnostic_index = self._diagnostic_index.as_unit("ns")
            self._refresh_analysis_regions()
            self._static_pixmap = None
            self._static_key = None
            self._edge_feedback = None
            if self._view_start is None and not data.empty:
                self._view_start = _timestamp(data.index[0])
                self._view_end = _timestamp(data.index[-1])
            self.update()

        def _analysis_diagnostic(self) -> pd.DataFrame:
            if self._diagnostic.empty:
                return self._diagnostic
            if self._period_start is None or self._period_end is None:
                return self._diagnostic.iloc[0:0]
            left = int(self._diagnostic_index.searchsorted(
                self._period_start, side="left"))
            right = int(self._diagnostic_index.searchsorted(
                self._period_end, side="right"))
            return self._diagnostic.iloc[left:right]

        def _refresh_analysis_regions(self) -> None:
            diagnostic = self._analysis_diagnostic()
            self._regions = (contiguous_regions(diagnostic["regime"])
                             if "regime" in diagnostic else [])

        def _view_limits(self) -> Tuple[pd.Timestamp, pd.Timestamp, pd.Timedelta]:
            first = _timestamp(self._data.index[0])
            last = _timestamp(self._data.index[-1])
            minimum = self._minimum_span()
            history = max(minimum, last - first)
            return first, last, history

        def _clamp_view(self, start: pd.Timestamp,
                        end: pd.Timestamp) -> Tuple[pd.Timestamp, pd.Timestamp]:
            if self._data.empty:
                return start, end
            lower, upper, maximum = self._view_limits()
            minimum = self._minimum_span()
            span = min(max(end - start, minimum), maximum)
            center = start + (end - start) / 2
            start, end = center - span / 2, center + span / 2
            if start < lower:
                end += lower - start
                start = lower
            if end > upper:
                start -= end - upper
                end = upper
            return max(lower, start), min(upper, end)

        def set_period(self, start: Any, end: Any, all_period: bool = False,
                       reset_view: bool = True) -> None:
            if self._data.empty:
                return
            first, last = _timestamp(self._data.index[0]), _timestamp(self._data.index[-1])
            requested_start, requested_end = _period_bounds(start, end)
            start_ts, end_ts = requested_start, requested_end
            if start_ts >= end_ts:
                end_ts = start_ts + pd.Timedelta(seconds=self._bar_seconds)
            self._period_start, self._period_end = start_ts, end_ts
            if reset_view:
                visible_start = max(first, start_ts)
                visible_end = min(last, end_ts)
                if visible_start >= visible_end:
                    visible_start, visible_end = first, last
                self._view_start, self._view_end = visible_start, visible_end
                self._focus_time = visible_start + (visible_end - visible_start) / 2
                self._focus_ratio = 0.5
            self._refresh_analysis_regions()
            if reset_view:
                self.viewChanged.emit(self._view_start, self._view_end)
            self._static_pixmap = None
            self._static_key = None
            self.update()

        def backtest_period(self) -> Tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
            return self._period_start, self._period_end

        def selected_period(self) -> Tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
            if self._data.empty or self._view_start is None or self._view_end is None:
                return self._period_start, self._period_end
            first, last = _timestamp(self._data.index[0]), _timestamp(self._data.index[-1])
            start = min(last, max(first, self._view_start))
            end = min(last, max(first, self._view_end))
            return (start, end) if start <= end else (end, start)

        def set_options(self, log_scale: Optional[bool] = None,
                        show_regime: Optional[bool] = None,
                        show_macd: Optional[bool] = None,
                        show_conditions: Optional[bool] = None) -> None:
            if log_scale is not None:
                self._log_scale = bool(log_scale)
            if show_regime is not None:
                self._show_regime = bool(show_regime)
            if show_macd is not None:
                self._show_macd = bool(show_macd)
            if show_conditions is not None:
                self._show_conditions = bool(show_conditions)
            self._static_pixmap = None
            self._static_key = None
            self.update()

        def _layout(self) -> Tuple[Any, Any, Any]:
            outer = self.rect().adjusted(62, 18, -18, -36)
            ribbon_height = 28 if self._show_conditions else 0
            macd_height = int(outer.height() * self._macd_fraction) if self._show_macd else 0
            gap = 10 if macd_height else 0
            price = QtCore.QRectF(
                outer.left(), outer.top(), outer.width(),
                max(100, outer.height() - ribbon_height - macd_height - gap))
            ribbon = QtCore.QRectF(
                outer.left(), price.bottom(), outer.width(), ribbon_height)
            macd = QtCore.QRectF(
                outer.left(), ribbon.bottom() + gap, outer.width(), macd_height)
            return price, ribbon, macd

        def _macd_splitter_rect(self) -> Any:
            _price, ribbon, macd = self._layout()
            if not self._show_macd or macd.height() <= 0:
                return QtCore.QRectF()
            return QtCore.QRectF(macd.left(), macd.top() - 6,
                                 macd.width(), 10)

        def _visible(self) -> pd.DataFrame:
            if self._data.empty or self._view_start is None or self._view_end is None:
                return self._data.iloc[0:0]
            left = int(self._data_index.searchsorted(self._view_start, side="left"))
            right = int(self._data_index.searchsorted(self._view_end, side="right"))
            return self._data.iloc[left:right]

        def _visible_diagnostic(self) -> pd.DataFrame:
            diagnostic = self._diagnostic
            if diagnostic.empty or self._view_start is None or self._view_end is None:
                return diagnostic.iloc[0:0]
            left = int(self._diagnostic_index.searchsorted(
                self._view_start, side="left"))
            right = int(self._diagnostic_index.searchsorted(
                self._view_end, side="right"))
            return diagnostic.iloc[left:right]

        def _visible_analysis_diagnostic(self) -> pd.DataFrame:
            diagnostic = self._analysis_diagnostic()
            if diagnostic.empty or self._view_start is None or self._view_end is None:
                return diagnostic.iloc[0:0]
            index = pd.DatetimeIndex([_timestamp(value) for value in diagnostic.index])
            if hasattr(index, "as_unit"):
                index = index.as_unit("ns")
            left = int(index.searchsorted(self._view_start, side="left"))
            right = int(index.searchsorted(self._view_end, side="right"))
            return diagnostic.iloc[left:right]

        def _x(self, stamp: Any, rect: Any) -> float:
            start, end = self._view_start, self._view_end
            if start is None or end is None or end <= start:
                return rect.left()
            ratio = (_timestamp(stamp) - start).total_seconds() / (
                end - start).total_seconds()
            return rect.left() + ratio * rect.width()

        def _time_at(self, x: float, rect: Any) -> pd.Timestamp:
            if self._view_start is None or self._view_end is None:
                # The widget can receive a paint/mouse event before the
                # background loader has supplied its first frame.
                return _timestamp(pd.Timestamp.now(tz="UTC"))
            ratio = float(np.clip((x - rect.left()) / max(rect.width(), 1.0), 0.0, 1.0))
            return self._view_start + (self._view_end - self._view_start) * ratio

        def _price_domain(self, visible: pd.DataFrame) -> Tuple[float, float]:
            values = []
            if not visible.empty:
                values.extend(pd.to_numeric(visible["low"], errors="coerce").tolist())
                values.extend(pd.to_numeric(visible["high"], errors="coerce").tolist())
            values = [float(v) for v in values if _finite(v) and float(v) > 0]
            if not values:
                return 1.0, 2.0
            low, high = min(values), max(values)
            if self._log_scale:
                lo, hi = np.log(low), np.log(high)
                pad = max((hi - lo) * 0.08, 0.02)
                return float(np.exp(lo - pad)), float(np.exp(hi + pad))
            pad = max((high - low) * 0.08, high * 0.01)
            return max(1e-12, low - pad), high + pad

        def _y(self, price: float, rect: Any, domain: Tuple[float, float]) -> float:
            low, high = domain
            if self._log_scale:
                value, lo, hi = np.log(max(price, 1e-12)), np.log(low), np.log(high)
            else:
                value, lo, hi = price, low, high
            ratio = (value - lo) / max(hi - lo, 1e-12)
            return rect.bottom() - ratio * rect.height()

        def _price_at(self, y: float, rect: Any,
                      domain: Tuple[float, float]) -> float:
            ratio = float(np.clip((rect.bottom() - y) / max(rect.height(), 1.0), 0, 1))
            low, high = domain
            if self._log_scale:
                return float(np.exp(np.log(low) + ratio * (np.log(high) - np.log(low))))
            return float(low + ratio * (high - low))

        def _draw_grid(self, painter: Any, rect: Any, domain: Tuple[float, float]) -> None:
            painter.save()
            painter.setPen(QtGui.QPen(QtGui.QColor("#273246"), 1))
            for i in range(7):
                y = rect.top() + rect.height() * i / 6
                painter.drawLine(QtCore.QPointF(rect.left(), y), QtCore.QPointF(rect.right(), y))
                value = self._price_at(y, rect, domain)
                painter.setPen(QtGui.QColor("#9AA7B8"))
                painter.drawText(QtCore.QRectF(2, y - 9, 56, 18),
                                 int(QtCore.Qt.AlignmentFlag.AlignRight |
                                     QtCore.Qt.AlignmentFlag.AlignVCenter),
                                 f"{value:,.0f}")
                painter.setPen(QtGui.QPen(QtGui.QColor("#273246"), 1))
            for i in range(9):
                x = rect.left() + rect.width() * i / 8
                painter.drawLine(QtCore.QPointF(x, rect.top()), QtCore.QPointF(x, rect.bottom()))
                stamp = self._time_at(x, rect)
                painter.setPen(QtGui.QColor("#9AA7B8"))
                painter.drawText(QtCore.QRectF(x - 45, rect.bottom() + 8, 90, 18),
                                 int(QtCore.Qt.AlignmentFlag.AlignCenter),
                                 stamp.strftime("%Y-%m"))
                painter.setPen(QtGui.QPen(QtGui.QColor("#273246"), 1))
            painter.restore()

        def _draw_regime_background(self, painter: Any, rect: Any) -> None:
            if not self._show_regime:
                return
            for region in self._regions:
                start, end = _timestamp(region["start"]), _timestamp(region["end"])
                if end < self._view_start or start > self._view_end:
                    continue
                x1 = self._x(max(start, self._view_start), rect)
                x2 = self._x(min(
                    end + pd.Timedelta(seconds=self._bar_seconds), self._view_end), rect)
                color = QtGui.QColor(REGIME_COLORS.get(region["label"], "#64748B"))
                color.setAlpha(24 if region["label"] != "판정 준비" else 12)
                painter.fillRect(QtCore.QRectF(x1, rect.top(), max(1.0, x2 - x1), rect.height()), color)

        def _draw_price(self, painter: Any, rect: Any, visible: pd.DataFrame,
                        diagnostic: pd.DataFrame, domain: Tuple[float, float]) -> None:
            if visible.empty:
                return
            painter.save()
            painter.setClipRect(rect)
            candles = len(visible) <= max(260, int(rect.width() / 2))
            width = max(1.0, min(9.0, rect.width() / max(len(visible), 1) * 0.65))
            if candles:
                for stamp, row in visible.iterrows():
                    if not all(_finite(row.get(name)) for name in ("open", "high", "low", "close")):
                        continue
                    x = self._x(stamp, rect)
                    up = float(row["close"]) >= float(row["open"])
                    color = QtGui.QColor("#19C79A" if up else "#F05B68")
                    painter.setPen(QtGui.QPen(color, 1))
                    painter.drawLine(QtCore.QPointF(x, self._y(float(row["high"]), rect, domain)),
                                     QtCore.QPointF(x, self._y(float(row["low"]), rect, domain)))
                    y1 = self._y(float(row["open"]), rect, domain)
                    y2 = self._y(float(row["close"]), rect, domain)
                    painter.fillRect(QtCore.QRectF(x - width / 2, min(y1, y2),
                                                   width, max(1.0, abs(y2 - y1))), color)
            else:
                stride = max(1, int(np.ceil(len(visible) / max(rect.width() * 2, 1))))
                visible = visible.iloc[::stride]
                path = QtGui.QPainterPath()
                first = True
                for stamp, value in visible["close"].items():
                    if not _finite(value):
                        continue
                    point = QtCore.QPointF(self._x(stamp, rect), self._y(float(value), rect, domain))
                    path.moveTo(point) if first else path.lineTo(point)
                    first = False
                painter.setPen(QtGui.QPen(QtGui.QColor("#DCE7F5"), 1.4))
                painter.drawPath(path)

            pen_styles = {
                "solid": QtCore.Qt.PenStyle.SolidLine,
                "dash": QtCore.Qt.PenStyle.DashLine,
                "dot": QtCore.Qt.PenStyle.DotLine,
            }
            for column, _label, color, style_key in OVERLAY_SERIES:
                style = pen_styles[style_key]
                if column not in diagnostic or not self._series_visible.get(column, True):
                    continue
                stride = max(1, int(np.ceil(len(diagnostic) / max(rect.width() * 2, 1))))
                series = diagnostic[column].iloc[::stride]
                path = QtGui.QPainterPath()
                first = True
                for stamp, value in series.items():
                    if not _finite(value) or float(value) <= 0:
                        first = True
                        continue
                    point = QtCore.QPointF(self._x(stamp, rect), self._y(float(value), rect, domain))
                    path.moveTo(point) if first else path.lineTo(point)
                    first = False
                painter.setPen(QtGui.QPen(QtGui.QColor(color), 1.25, style))
                painter.drawPath(path)
            painter.restore()

        def _draw_legend(self, painter: Any, rect: Any) -> None:
            """
            좌상단 보조선 스위치.

            범례와 토글을 겸합니다. 꺼진 선은 흐리게 보여 "그 선이 없는 것"과
            "끈 것"을 구분할 수 있게 합니다.
            """
            painter.save()
            metrics = painter.fontMetrics()
            pad, gap, swatch = 7, 4, 15
            rows = []
            width = 0
            for column, label, color, style_key in OVERLAY_SERIES:
                text_w = metrics.horizontalAdvance(label)
                rows.append((column, label, color, style_key, text_w))
                width = max(width, swatch + 6 + text_w)
            row_h = max(16, metrics.height() + 2)
            box_w = width + pad * 2
            box_h = row_h * len(rows) + pad * 2
            left, top = rect.left() + 8, rect.top() + 26
            box = QtCore.QRectF(left, top, box_w, box_h)
            backdrop = QtGui.QColor("#0F1724")
            backdrop.setAlpha(196)
            painter.fillRect(box, backdrop)
            painter.setPen(QtGui.QPen(QtGui.QColor("#273246"), 1))
            painter.drawRect(box)

            pen_styles = {
                "solid": QtCore.Qt.PenStyle.SolidLine,
                "dash": QtCore.Qt.PenStyle.DashLine,
                "dot": QtCore.Qt.PenStyle.DotLine,
            }
            self._legend_hit = {}
            for index, (column, label, color, style_key, _tw) in enumerate(rows):
                y = top + pad + index * row_h
                hit = QtCore.QRectF(left, y, box_w, row_h)
                self._legend_hit[column] = hit
                on = self._series_visible.get(column, True)
                line = QtGui.QColor(color)
                if not on:
                    line.setAlpha(70)
                painter.setPen(QtGui.QPen(line, 2, pen_styles[style_key]))
                mid = y + row_h / 2
                painter.drawLine(QtCore.QPointF(left + pad, mid),
                                 QtCore.QPointF(left + pad + swatch, mid))
                painter.setPen(QtGui.QColor("#DDE7F4" if on else "#5A6472"))
                painter.drawText(
                    QtCore.QRectF(left + pad + swatch + 6, y, box_w, row_h),
                    int(QtCore.Qt.AlignmentFlag.AlignLeft
                        | QtCore.Qt.AlignmentFlag.AlignVCenter), label)
            painter.restore()

        def _legend_column_at(self, point: Any) -> Optional[str]:
            for column, rect in self._legend_hit.items():
                if rect.contains(point):
                    return column
            return None

        def _draw_ribbons(self, painter: Any, rect: Any, diagnostic: pd.DataFrame) -> None:
            if not self._show_conditions or rect.height() <= 0 or diagnostic.empty:
                return
            columns = [("bull_signal", "상승 판정"),
                       ("bear_signal", "하락 판정")]
            row_height = rect.height() / len(columns)
            for row, (column, label) in enumerate(columns):
                y = rect.top() + row * row_height
                painter.setPen(QtGui.QColor("#9AA7B8"))
                painter.drawText(QtCore.QRectF(4, y, 54, row_height),
                                 int(QtCore.Qt.AlignmentFlag.AlignRight |
                                     QtCore.Qt.AlignmentFlag.AlignVCenter), label)
                states = diagnostic[column].map(
                    lambda value: "on" if pd.notna(value) and bool(value)
                    else "off" if pd.notna(value) else np.nan)
                for region in contiguous_regions(states):
                    x1 = self._x(region["start"], rect)
                    x2 = self._x(
                        _timestamp(region["end"])
                        + pd.Timedelta(seconds=self._bar_seconds), rect)
                    active_color = "#10B981" if column == "bull_signal" else "#EF4444"
                    color = (QtGui.QColor(active_color) if region["label"] == "on"
                             else QtGui.QColor("#64748B"))
                    color.setAlpha(150)
                    painter.fillRect(QtCore.QRectF(x1, y + 1, max(1, x2 - x1),
                                                   max(1, row_height - 2)), color)

        def _draw_macd(self, painter: Any, rect: Any, diagnostic: pd.DataFrame) -> None:
            if not self._show_macd or rect.height() <= 0 or diagnostic.empty:
                return
            values = pd.concat([
                diagnostic["log_macd"], diagnostic["log_macd_signal"],
                diagnostic["log_macd_histogram"],
            ]).replace([np.inf, -np.inf], np.nan).dropna()
            if values.empty:
                return
            limit = max(abs(float(values.min())), abs(float(values.max())), 1e-9)
            zero = rect.center().y()
            painter.setPen(QtGui.QPen(QtGui.QColor("#3A465A"), 1))
            painter.drawRect(rect)
            painter.drawLine(QtCore.QPointF(rect.left(), zero), QtCore.QPointF(rect.right(), zero))

            def my(value: float) -> float:
                return zero - float(value) / limit * rect.height() * 0.46

            stride = max(1, int(np.ceil(len(diagnostic) / max(rect.width(), 1))))
            for stamp, value in diagnostic["log_macd_histogram"].iloc[::stride].items():
                if not _finite(value):
                    continue
                x = self._x(stamp, rect)
                color = QtGui.QColor("#4ADE80" if float(value) >= 0 else "#FB7185")
                painter.setPen(QtGui.QPen(color, 2))
                painter.drawLine(QtCore.QPointF(x, zero), QtCore.QPointF(x, my(float(value))))
            for column, color in (("log_macd", "#38BDF8"),
                                  ("log_macd_signal", "#F97316")):
                series = diagnostic[column].iloc[::max(
                    1, int(np.ceil(len(diagnostic) / max(rect.width() * 2, 1))))]
                path = QtGui.QPainterPath()
                first = True
                for stamp, value in series.items():
                    if not _finite(value):
                        continue
                    point = QtCore.QPointF(self._x(stamp, rect), my(float(value)))
                    path.moveTo(point) if first else path.lineTo(point)
                    first = False
                painter.setPen(QtGui.QPen(QtGui.QColor(color), 1.2))
                painter.drawPath(path)
            painter.setPen(QtGui.QColor("#AAB6C6"))
            painter.drawText(rect.adjusted(6, 2, -6, -2),
                             int(QtCore.Qt.AlignmentFlag.AlignTop |
                                 QtCore.Qt.AlignmentFlag.AlignLeft),
                             "로그가격 MACD")

        def _draw_crosshair(self, painter: Any, price_rect: Any,
                            domain: Tuple[float, float]) -> None:
            if self._crosshair is None or not price_rect.contains(self._crosshair):
                return
            x, y = self._crosshair.x(), self._crosshair.y()
            painter.setPen(QtGui.QPen(QtGui.QColor("#8B98AA"), 1,
                                     QtCore.Qt.PenStyle.DashLine))
            painter.drawLine(QtCore.QPointF(x, price_rect.top()),
                             QtCore.QPointF(x, price_rect.bottom()))
            painter.drawLine(QtCore.QPointF(price_rect.left(), y),
                             QtCore.QPointF(price_rect.right(), y))
            stamp = self._time_at(x, price_rect)
            price = self._price_at(y, price_rect, domain)
            nearest = None
            if not self._diagnostic.empty:
                nearest = int(np.argmin(np.abs((self._diagnostic_index - stamp).asi8)))
            detail = f"{stamp:%Y-%m-%d}  ${price:,.2f}"
            if nearest is not None:
                row = self._diagnostic.iloc[nearest]
                detail += (f"  {row.get('regime', '—')}  방향 {row.get('direction_score', np.nan):.1f}"
                           f"  확장 {row.get('expansion_score', np.nan):.1f}")
            metrics = painter.fontMetrics()
            width = metrics.horizontalAdvance(detail) + 16
            box_x = min(max(price_rect.left(), x + 10), price_rect.right() - width)
            box = QtCore.QRectF(box_x, price_rect.top() + 8, width, 24)
            painter.fillRect(box, QtGui.QColor("#111827"))
            painter.setPen(QtGui.QColor("#E5EDF7"))
            painter.drawText(box, int(QtCore.Qt.AlignmentFlag.AlignCenter), detail)

        def paintEvent(self, _event: Any) -> None:
            if self._view_start is None or self._view_end is None:
                painter = QtGui.QPainter(self)
                painter.fillRect(self.rect(), QtGui.QColor("#0F1724"))
                painter.setPen(QtGui.QColor("#AAB6C6"))
                painter.drawText(
                    self.rect(), int(QtCore.Qt.AlignmentFlag.AlignCenter),
                    "글로벌 BTC 데이터 읽는 중…")
                painter.end()
                return
            price_rect, ribbon_rect, macd_rect = self._layout()
            visible = self._visible()
            diagnostic = self._visible_diagnostic()
            analysis_diagnostic = self._visible_analysis_diagnostic()
            domain = self._price_domain(visible)
            cache_key = (
                self.width(), self.height(), self._view_start, self._view_end,
                self._log_scale, self._show_regime, self._show_macd,
                self._show_conditions, id(self._data), id(self._diagnostic),
                tuple(sorted(self._series_visible.items())),
            )
            if self._static_pixmap is None or cache_key != self._static_key:
                pixmap = QtGui.QPixmap(self.size())
                pixmap.fill(QtGui.QColor("#0F1724"))
                base = QtGui.QPainter(pixmap)
                base.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
                self._draw_regime_background(base, price_rect)
                self._draw_grid(base, price_rect, domain)
                self._draw_price(base, price_rect, visible, diagnostic, domain)
                self._draw_ribbons(base, ribbon_rect, analysis_diagnostic)
                self._draw_legend(base, price_rect)
                self._draw_macd(base, macd_rect, diagnostic)
                if self._show_macd:
                    handle = self._macd_splitter_rect()
                    base.fillRect(handle, QtGui.QColor("#273246"))
                    base.setPen(QtGui.QPen(QtGui.QColor("#64748B"), 1))
                    base.drawLine(QtCore.QPointF(handle.left(), handle.center().y()),
                                  QtCore.QPointF(handle.right(), handle.center().y()))
                base.setPen(QtGui.QColor("#DDE7F4"))
                base.drawText(QtCore.QRectF(price_rect.left() + 8, 0, 460, 22),
                              int(QtCore.Qt.AlignmentFlag.AlignLeft |
                                  QtCore.Qt.AlignmentFlag.AlignVCenter),
                              f"BTC/USD · 글로벌 UTC {self._interval_label} · "
                              f"{'로그' if self._log_scale else '선형'}")
                base.end()
                self._static_pixmap, self._static_key = pixmap, cache_key
            painter = QtGui.QPainter(self)
            painter.drawPixmap(0, 0, self._static_pixmap)
            if (self._drag_origin is not None
                    and abs(self._drag_preview_dx) >= 1.0):
                # Move only the already-rendered plot snapshot while the mouse
                # is held.  Indicators/data are recomputed once on mouse-up.
                content_bottom = (
                    macd_rect.bottom() if self._show_macd
                    else ribbon_rect.bottom() if self._show_conditions
                    else price_rect.bottom())
                content = QtCore.QRectF(
                    price_rect.left(), price_rect.top(), price_rect.width(),
                    max(1.0, content_bottom - price_rect.top()))
                painter.save()
                painter.setClipRect(content)
                painter.fillRect(content, QtGui.QColor("#0F1724"))
                source = self._static_pixmap.copy(
                    int(content.left()), int(content.top()),
                    max(1, int(content.width())), max(1, int(content.height())))
                painter.drawPixmap(int(round(content.left() + self._drag_preview_dx)),
                                   int(content.top()), source)
                painter.restore()
            if self._edge_feedback:
                at_left = self._edge_feedback == "left"
                x = price_rect.left() if at_left else price_rect.right() - 4
                color = QtGui.QColor("#F5B03E")
                painter.fillRect(QtCore.QRectF(x, price_rect.top(), 4,
                                               price_rect.height()), color)
                label = "데이터 시작" if at_left else "데이터 끝"
                label_rect = QtCore.QRectF(
                    price_rect.left() + 10 if at_left else price_rect.right() - 100,
                    price_rect.center().y() - 14, 90, 28)
                painter.fillRect(label_rect, QtGui.QColor("#111827"))
                painter.setPen(color)
                painter.drawText(label_rect, int(QtCore.Qt.AlignmentFlag.AlignCenter), label)
            self._draw_crosshair(painter, price_rect, domain)
            painter.end()

        def mousePressEvent(self, event: Any) -> None:
            point = _event_pos(event)
            price_rect, _, _ = self._layout()
            if event.button() == QtCore.Qt.MouseButton.LeftButton:
                column = self._legend_column_at(point)
                if column is not None:
                    # 범례 클릭은 화면 이동으로 넘기지 않습니다.
                    self._series_visible[column] = not self._series_visible.get(
                        column, True)
                    self._static_pixmap = None
                    self._static_key = None
                    self.update()
                    event.accept()
                    return
            if (event.button() == QtCore.Qt.MouseButton.LeftButton
                    and self._macd_splitter_rect().contains(point)):
                self._splitter_dragging = True
                self.setCursor(QtCore.Qt.CursorShape.SplitVCursor)
                event.accept()
                return
            if event.button() == QtCore.Qt.MouseButton.LeftButton and price_rect.contains(point):
                if (self._data.empty or self._view_start is None
                        or self._view_end is None):
                    self._drag_origin = self._drag_range = None
                    super().mousePressEvent(event)
                    return
                self._focus_time = self._time_at(point.x(), price_rect)
                self._focus_ratio = float(np.clip(
                    (point.x() - price_rect.left()) / max(price_rect.width(), 1.0),
                    0.0, 1.0))
                self._drag_origin = point
                self._drag_range = (self._view_start, self._view_end)
                self._drag_last_point = point
                self._drag_preview_dx = 0.0
                self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            super().mousePressEvent(event)

        def mouseMoveEvent(self, event: Any) -> None:
            point = _event_pos(event)
            if self._splitter_dragging:
                outer = self.rect().adjusted(62, 18, -18, -36)
                fraction = (outer.bottom() - point.y()) / max(outer.height(), 1)
                self._macd_fraction = float(np.clip(fraction, 0.12, 0.55))
                self._static_pixmap = None
                self._static_key = None
                self.update()
                event.accept()
                return
            self._crosshair = point
            self._preview_drag_to(point)
            self.update()
            super().mouseMoveEvent(event)

        def _preview_drag_to(self, point: Any) -> None:
            if self._drag_origin is not None and self._drag_range is not None:
                # Keep the current candles fixed while dragging.  Applying the
                # final delta only on mouse-up avoids repeated data/render work
                # and prevents partially initialised drag ranges from leaking
                # into the time-axis calculation.
                self._drag_last_point = point
                self._drag_preview_dx = float(point.x() - self._drag_origin.x())

        def mouseReleaseEvent(self, event: Any) -> None:
            point = _event_pos(event)
            if self._splitter_dragging:
                self._splitter_dragging = False
                self.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
                self.update()
                event.accept()
                return
            if self._drag_origin is not None and self._drag_range is not None:
                start, end = self._drag_range
                price_rect, _, _ = self._layout()
                if start is not None and end is not None:
                    seconds = (end - start).total_seconds()
                    delta = -(point.x() - self._drag_origin.x()) / max(
                        price_rect.width(), 1.0) * seconds
                    desired_start = start + pd.Timedelta(seconds=delta)
                    desired_end = end + pd.Timedelta(seconds=delta)
                    first, last, _maximum = self._view_limits()
                    self._edge_feedback = (
                        "left" if desired_start < first else
                        "right" if desired_end > last else None)
                    self._view_start, self._view_end = self._clamp_view(
                        desired_start, desired_end)
                    self.viewChanged.emit(self._view_start, self._view_end)
                    if self._edge_feedback:
                        self.windowRequested.emit(desired_start, desired_end)
                if (self._view_start is not None and self._view_end is not None
                        and price_rect.contains(point)):
                    self._focus_time = self._time_at(point.x(), price_rect)
                    self._focus_ratio = float(np.clip(
                        (point.x() - price_rect.left()) / max(price_rect.width(), 1.0),
                        0.0, 1.0))
            self._drag_origin = self._drag_range = self._drag_last_point = None
            self._drag_preview_dx = 0.0
            self.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
            self.update()
            super().mouseReleaseEvent(event)

        def leaveEvent(self, event: Any) -> None:
            self._crosshair = None
            self.update()
            super().leaveEvent(event)

        def wheelEvent(self, event: Any) -> None:
            if self._view_start is None or self._view_end is None:
                return
            price_rect, _, _ = self._layout()
            point = _event_pos(event)
            anchor = self._time_at(point.x(), price_rect)
            factor = 0.82 if event.angleDelta().y() > 0 else 1.22
            minimum = self._minimum_span()
            _, _, maximum = self._view_limits()
            old_span = self._view_end - self._view_start
            new_span = min(max(old_span * factor, minimum), maximum)
            ratio = float(np.clip(
                (anchor - self._view_start) / max(old_span, pd.Timedelta(seconds=1)),
                0.0, 1.0))
            new_start = anchor - new_span * ratio
            new_end = new_start + new_span
            first, last, _maximum = self._view_limits()
            needs_more = new_start < first or new_end > last
            # 가장자리를 벗어나지 않는 확대·축소는 "데이터 끝" 표시를 지웁니다.
            self._edge_feedback = ("left" if new_start < first else
                                   "right" if new_end > last else None)
            self._view_start, self._view_end = self._clamp_view(new_start, new_end)
            self._focus_time, self._focus_ratio = anchor, ratio
            self.viewChanged.emit(self._view_start, self._view_end)
            if needs_more:
                self.windowRequested.emit(new_start, new_end)
            self.update()
            event.accept()

    class _Loader(QtCore.QObject):
        finished = QtCore.pyqtSignal(object)

        def __init__(self, generation: int, interval: str,
                     start: Optional[Any] = None, end: Optional[Any] = None,
                     anchor: Optional[Mapping[str, Any]] = None,
                     visible_span: Optional[pd.Timedelta] = None,
                     view_revision: int = 0, full_period: bool = False):
            super().__init__()
            self.generation = generation
            self.interval = interval
            self.start = start
            self.end = end
            self.anchor = dict(anchor) if anchor else None
            self.visible_span = visible_span
            self.view_revision = int(view_revision)
            self.full_period = bool(full_period)

        def run(self) -> None:
            try:
                from global_market_data import ensure_global_btc_current, load_global_btc
                ensure_global_btc_current(lock_timeout=1.0)
                source_interval = "1d" if self.interval in {"1w", "1mo"} else self.interval
                start, end = self.start, self.end
                if self.interval == "1w" and start is not None:
                    start = _timestamp(start)
                    start -= pd.Timedelta(days=start.weekday())
                elif self.interval == "1mo" and start is not None:
                    stamp = _timestamp(start)
                    start = pd.Timestamp(stamp.year, stamp.month, 1)
                data = load_global_btc(source_interval, start=start, end=end)
                data = aggregate_chart_frame(data, self.interval)
                if data.empty:
                    from global_market_data import bootstrap_needed
                    if bootstrap_needed(interval=source_interval):
                        # 한 번도 받은 적이 없는 상태.  CLI 를 실행하라고 안내만
                        # 하면 아무도 못 알아봅니다.  창이 직접 받아 옵니다.
                        self.finished.emit({
                            "ok": False, "needs_bootstrap": True,
                            "generation": self.generation,
                            "interval": self.interval,
                            "message": "공용 BTC 정본이 없습니다",
                        })
                        return
                    raise RuntimeError(
                        "요청한 구간에 공용 BTC 데이터가 없습니다. "
                        "기간을 조정하거나 새로고침해 주세요.")
                self.finished.emit({
                    "ok": True, "data": data, "generation": self.generation,
                    "interval": self.interval, "anchor": self.anchor,
                    "visible_span": self.visible_span,
                    "view_revision": self.view_revision,
                    "full_period": self.full_period,
                })
            except Exception as exc:
                self.finished.emit({"ok": False, "message": str(exc),
                                    "generation": self.generation,
                                    "interval": self.interval})

    class _BootstrapWorker(QtCore.QObject):
        """공용 BTC 정본을 처음 받아 오는 백그라운드 작업."""

        progress = QtCore.pyqtSignal(object)
        finished = QtCore.pyqtSignal(object)

        def __init__(self):
            super().__init__()
            self._cancelled = False

        def cancel(self) -> None:
            self._cancelled = True

        def run(self) -> None:
            try:
                from global_market_data import (ArchiveCancelled,
                                                BitstampBTCArchive,
                                                parse_progress)
                archive = BitstampBTCArchive()
                result = archive.run(
                    workers=4,
                    progress=lambda message: self.progress.emit(
                        parse_progress(message)),
                    should_cancel=lambda: self._cancelled,
                )
                self.finished.emit({"ok": True, "result": result})
            except Exception as exc:
                from global_market_data import ArchiveCancelled
                self.finished.emit({
                    "ok": False,
                    "cancelled": isinstance(exc, ArchiveCancelled),
                    "message": str(exc),
                })

    class RegimeChartWindow(QtWidgets.QWidget):
        def __init__(self):
            super().__init__(parent, QtCore.Qt.WindowType.Window)
            import ui_theme
            self.setStyleSheet(ui_theme.stylesheet())
            self.setWindowTitle("QuantBot · BTC 국면 차트")
            self.setMinimumSize(920, 620)
            self.resize(1280, 780)
            ui_theme.fit_available_height(self, 1280)
            self._data = pd.DataFrame()
            self._load_generation = 0
            self._interval_buttons: Dict[str, Any] = {}
            self._view_states: Dict[str, Dict[str, Any]] = {}
            self._view_revision = 0
            self._loaded_full_period = False
            self._bootstrap_worker = None
            self._bootstrap_thread = None
            self._config = dict(config_provider())
            self._score = scoring_config(self._config)
            self._current_interval = str(self._score.get("decision_interval", "1d"))
            self._requested_interval = self._current_interval
            self._building = True
            self._debounce = QtCore.QTimer(self)
            self._debounce.setSingleShot(True)
            self._debounce.setInterval(250)
            self._debounce.timeout.connect(self._recalculate)
            self._build_ui()
            self._load_controls()
            self._building = False
            self._load_data()

        def _build_ui(self) -> None:
            import ui_theme
            outer = QtWidgets.QVBoxLayout(self)
            outer.setContentsMargins(14, 12, 14, 12)
            top = QtWidgets.QHBoxLayout()
            title = QtWidgets.QLabel("BTC 글로벌 국면 분석")
            title.setObjectName("Title")
            top.addWidget(title)
            top.addStretch(1)
            self.status = QtWidgets.QLabel("데이터 읽는 중…")
            self.status.setObjectName("HintStrong")
            top.addWidget(self.status)
            self.log_check = QtWidgets.QCheckBox("로그 가격")
            self.log_check.setChecked(True)
            top.addWidget(self.log_check)
            self.macd_check = QtWidgets.QCheckBox("로그 MACD")
            self.macd_check.setChecked(True)
            top.addWidget(self.macd_check)
            reload_button = QtWidgets.QPushButton("새로고침")
            reload_button.clicked.connect(self._load_data)
            top.addWidget(reload_button)
            outer.addLayout(top)

            interval_row = QtWidgets.QHBoxLayout()
            interval_row.setSpacing(2)
            interval_row.addWidget(QtWidgets.QLabel("시간 간격"))
            self.interval_group = QtWidgets.QButtonGroup(self)
            self.interval_group.setExclusive(True)
            c = ui_theme.COLORS
            interval_style = (
                "QToolButton { background: transparent; color: " + c["text_dim"] +
                "; border: 0; border-radius: 5px; padding: 4px 7px; }"
                "QToolButton:hover { background: " + c["hover"] + "; color: " + c["text"] + "; }"
                "QToolButton:checked { background: " + c["elevated"] +
                "; color: " + c["text"] + "; }"
                "QToolButton:disabled { color: " + c["text_muted"] + "; }")
            for number, (key, label) in enumerate(CHART_INTERVALS):
                button = QtWidgets.QToolButton()
                button.setText(label)
                button.setCheckable(True)
                button.setStyleSheet(interval_style)
                button.setToolTip(f"BTC 차트를 {label} 간격으로 전환")
                button.setEnabled(False)
                button.clicked.connect(
                    lambda _checked=False, interval=key: self._change_interval(interval))
                self.interval_group.addButton(button, number)
                self._interval_buttons[key] = button
                interval_row.addWidget(button)
            self._interval_buttons[self._current_interval].setChecked(True)
            interval_row.addStretch(1)
            outer.addLayout(interval_row)

            # 정본이 없을 때만 나타나는 수집 진행 줄.
            self.bootstrap_row = QtWidgets.QWidget()
            bootstrap_layout = QtWidgets.QHBoxLayout(self.bootstrap_row)
            bootstrap_layout.setContentsMargins(0, 2, 0, 4)
            bootstrap_layout.setSpacing(8)
            self.bootstrap_label = QtWidgets.QLabel("")
            self.bootstrap_label.setObjectName("Hint")
            self.bootstrap_label.setMinimumWidth(280)
            bootstrap_layout.addWidget(self.bootstrap_label)
            self.bootstrap_bar = QtWidgets.QProgressBar()
            self.bootstrap_bar.setRange(0, 0)          # 처음엔 진행률을 모릅니다
            self.bootstrap_bar.setTextVisible(True)
            bootstrap_layout.addWidget(self.bootstrap_bar, 1)
            self.bootstrap_cancel = QtWidgets.QPushButton("중단")
            self.bootstrap_cancel.setObjectName("Ghost")
            self.bootstrap_cancel.clicked.connect(self._cancel_bootstrap)
            bootstrap_layout.addWidget(self.bootstrap_cancel)
            self.bootstrap_row.setVisible(False)
            outer.addWidget(self.bootstrap_row)

            splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
            self.chart = PriceChart()
            self.chart.windowRequested.connect(self._request_visible_window)
            splitter.addWidget(self.chart)
            panel = QtWidgets.QScrollArea()
            panel.setWidgetResizable(True)
            panel.setHorizontalScrollBarPolicy(
                QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            panel.setMinimumWidth(380)
            panel.setMaximumWidth(420)
            content = QtWidgets.QWidget()
            self.panel_layout = QtWidgets.QVBoxLayout(content)
            self.panel_layout.setContentsMargins(12, 10, 12, 10)
            self.panel_layout.setSpacing(10)
            panel.setWidget(content)
            splitter.addWidget(panel)
            splitter.setStretchFactor(0, 4)
            splitter.setStretchFactor(1, 1)
            outer.addWidget(splitter, 1)

            self.regime_check = QtWidgets.QCheckBox("국면 판정")
            self.regime_check.setChecked(True)
            self.regime_check.setToolTip(
                "차트의 국면 배경과 조건별 점수 계산 옵션을 켭니다. 실전략 적용은 아래에서 별도로 선택합니다.")
            self.panel_layout.addWidget(self.regime_check)
            self.condition_check = QtWidgets.QCheckBox("조건별 판정 띠")
            self.condition_check.setChecked(True)
            self.panel_layout.addWidget(self.condition_check)
            self.strategy_check = QtWidgets.QCheckBox(
                "장세별 전략을 백테스트에 적용 (실전 제외)")
            self.strategy_check.setToolTip(
                "백테스트에만 적용합니다. 저장해도 실전 봇 주문 규칙은 바뀌지 않습니다. "
                "상승·안정·하락에 선택한 전략을 각각 사용합니다.")
            self.panel_layout.addWidget(self.strategy_check)

            self.inputs: Dict[str, Any] = {}
            detector_group = QtWidgets.QGroupBox("장세 판정 방식")
            detector_form = QtWidgets.QFormLayout(detector_group)
            detector_form.setHorizontalSpacing(6)
            detector_form.setVerticalSpacing(5)
            detector_form.setFieldGrowthPolicy(
                QtWidgets.QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
            # 라벨 칸은 가장 긴 라벨 기준으로 잡히므로, 왼쪽 정렬이면 짧은 라벨이
            # 입력란에서 멀리 떨어져 보입니다. 오른쪽으로 붙입니다.
            detector_form.setLabelAlignment(
                QtCore.Qt.AlignmentFlag.AlignRight
                | QtCore.Qt.AlignmentFlag.AlignVCenter)
            for key, label in (("bull_detector", "상승장 찾기"),
                               ("bear_detector", "하락장 찾기")):
                combo = QtWidgets.QComboBox()
                options = (BULL_DETECTOR_OPTIONS if key == "bull_detector"
                           else BEAR_DETECTOR_OPTIONS)
                for value, text_label in options:
                    combo.addItem(text_label, value)
                self.inputs[key] = combo
                detector_form.addRow(label, combo)
            self.panel_layout.addWidget(detector_group)

            parameter_group = QtWidgets.QGroupBox("판정값")
            parameter_layout = QtWidgets.QVBoxLayout(parameter_group)
            parameter_layout.setSpacing(4)
            ma_row = QtWidgets.QHBoxLayout()
            ma_row.setSpacing(4)
            self.inputs["short_ma"] = QtWidgets.QSpinBox()
            self.inputs["short_ma"].setRange(5, 200)
            compact_spin(self.inputs["short_ma"])
            self.inputs["long_ma"] = QtWidgets.QSpinBox()
            self.inputs["long_ma"].setRange(20, 400)
            compact_spin(self.inputs["long_ma"])
            ma_row.addWidget(QtWidgets.QLabel("단기"))
            ma_row.addWidget(self.inputs["short_ma"])
            ma_row.addWidget(QtWidgets.QLabel("장기"))
            ma_row.addWidget(self.inputs["long_ma"])
            ma_row.addStretch(1)
            parameter_layout.addWidget(QtWidgets.QLabel("이중 이동평균 추세"))
            parameter_layout.addLayout(ma_row)

            macd_row = QtWidgets.QHBoxLayout()
            macd_row.setSpacing(4)
            for key, label in (("macd_fast", "빠름"), ("macd_slow", "느림"),
                               ("macd_signal", "신호")):
                widget = QtWidgets.QSpinBox()
                widget.setRange(2, 240)
                compact_spin(widget)
                self.inputs[key] = widget
                macd_row.addWidget(QtWidgets.QLabel(label))
                macd_row.addWidget(widget)
            macd_row.addStretch(1)
            parameter_layout.addWidget(QtWidgets.QLabel("로그 MACD"))
            parameter_layout.addLayout(macd_row)

            atr_row = QtWidgets.QHBoxLayout()
            atr_row.setSpacing(4)
            threshold = QtWidgets.QDoubleSpinBox()
            threshold.setRange(0.1, 20.0)
            threshold.setSingleStep(0.1)
            threshold.setDecimals(1)
            compact_spin(threshold)
            self.inputs["atr_multiple"] = threshold
            atr_row.addWidget(QtWidgets.QLabel("ATR %"))
            atr_row.addWidget(threshold)
            for key, label, default in (("bull_atr_window", "상승", 10),
                                        ("bear_atr_window", "하락", 2)):
                widget = QtWidgets.QSpinBox()
                widget.setRange(2, 120)
                widget.setValue(default)
                compact_spin(widget)
                self.inputs[key] = widget
                atr_row.addWidget(QtWidgets.QLabel(label))
                atr_row.addWidget(widget)
            atr_row.addStretch(1)
            parameter_layout.addWidget(QtWidgets.QLabel("ATR 변동성 판정"))
            parameter_layout.addLayout(atr_row)

            channel_row = QtWidgets.QHBoxLayout()
            channel_row.setSpacing(4)
            for key, label in (("breakout_lower_window", "기간"),
                               ("channel_slope_bars", "기울기")):
                widget = QtWidgets.QSpinBox()
                widget.setRange(2, 120)
                compact_spin(widget)
                self.inputs[key] = widget
                channel_row.addWidget(QtWidgets.QLabel(label))
                channel_row.addWidget(widget)
            channel_row.addStretch(1)
            parameter_layout.addWidget(QtWidgets.QLabel("하방선 각도·직선 연장"))
            parameter_layout.addLayout(channel_row)
            self.panel_layout.addWidget(parameter_group)

            strategy_group = QtWidgets.QGroupBox("장세별 백테스트 전략")
            strategy_form = QtWidgets.QFormLayout(strategy_group)
            strategy_form.setHorizontalSpacing(6)
            strategy_form.setVerticalSpacing(5)
            strategy_form.setFieldGrowthPolicy(
                QtWidgets.QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
            strategy_form.setLabelAlignment(
                QtCore.Qt.AlignmentFlag.AlignRight
                | QtCore.Qt.AlignmentFlag.AlignVCenter)
            for key, label in (("bull_strategy", "상승기"),
                               ("stable_strategy", "안정기"),
                               ("bear_strategy", "하락기")):
                combo = QtWidgets.QComboBox()
                for value, text_label in STRATEGY_OPTIONS:
                    combo.addItem(text_label, value)
                self.inputs[key] = combo
                strategy_form.addRow(label, combo)
            atr_combo = QtWidgets.QComboBox()
            for multiple in (2.0, 4.0, 6.0, 8.0):
                atr_combo.addItem(f"{multiple:g} ATR", multiple)
            self.inputs["defensive_atr_multiple"] = atr_combo
            strategy_form.addRow("ATR 하단 거리", atr_combo)
            entry_method = QtWidgets.QComboBox()
            entry_method.addItem("ATR 하단", "atr")
            entry_method.addItem("하방 채널선 돌파", "lower_channel")
            self.inputs["defensive_entry_method"] = entry_method
            strategy_form.addRow("예약매수 방식", entry_method)
            probe = QtWidgets.QDoubleSpinBox()
            probe.setRange(5.0, 100.0)
            probe.setSuffix(" %")
            probe.setDecimals(0)
            self.inputs["defensive_probe_fraction"] = probe
            strategy_form.addRow("예약매수 비중", probe)
            take_profit = QtWidgets.QDoubleSpinBox()
            take_profit.setRange(0.1, 100.0)
            take_profit.setSingleStep(0.5)
            take_profit.setSuffix(" %")
            take_profit.setDecimals(1)
            self.inputs["defensive_take_profit_pct"] = take_profit
            strategy_form.addRow("예약 체결분 익절", take_profit)
            stop_atr = QtWidgets.QDoubleSpinBox()
            stop_atr.setRange(0.1, 20.0)
            stop_atr.setSingleStep(0.1)
            stop_atr.setSuffix(" ATR")
            stop_atr.setDecimals(1)
            stop_atr.setToolTip(
                "예약 주문 기준선이 아니라 실제 체결가에서 이 ATR 배수만큼 내려가면 손절합니다.")
            self.inputs["defensive_stop_atr_multiple"] = stop_atr
            strategy_form.addRow("예약 체결분 손절", stop_atr)
            cancel_buffer = QtWidgets.QDoubleSpinBox()
            cancel_buffer.setRange(0.0, 10.0)
            cancel_buffer.setSingleStep(0.05)
            cancel_buffer.setSuffix(" ATR")
            cancel_buffer.setDecimals(2)
            self.inputs["defensive_cancel_buffer_atr"] = cancel_buffer
            strategy_form.addRow("돌파 접근 예약취소", cancel_buffer)
            self.panel_layout.addWidget(strategy_group)
            # 콤보와 스핀박스가 제각각 자기 글자 길이만큼 늘어나 계단처럼
            # 보였습니다. 두 그룹을 통틀어 **가장 긴 항목 하나**에 폭을 맞춥니다.
            # 고정값을 쓰면 긴 항목이 잘리므로 실제 필요한 폭에서 뽑습니다.
            self._align_field_widths(detector_form, strategy_form)

            self.summary = QtWidgets.QLabel("국면 계산 대기")
            self.summary.setWordWrap(True)
            self.summary.setObjectName("BacktestSummary")
            self.summary.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
                | QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard)
            self.summary.setCursor(QtCore.Qt.CursorShape.IBeamCursor)
            self.panel_layout.addWidget(self.summary)
            self.panel_layout.addStretch(1)

            bottom = QtWidgets.QHBoxLayout()
            self.period_label = QtWidgets.QLabel("")
            bottom.addWidget(self.period_label, 1)
            outer.addLayout(bottom)

            self.log_check.toggled.connect(
                lambda value: self.chart.set_options(log_scale=value))
            self.macd_check.toggled.connect(
                lambda value: self.chart.set_options(show_macd=value))
            self.regime_check.toggled.connect(self._regime_toggled)
            self.condition_check.toggled.connect(
                lambda value: self.chart.set_options(show_conditions=value))
            self.strategy_check.toggled.connect(self._schedule)
            self.chart.viewChanged.connect(self._view_changed)
            for widget in self.inputs.values():
                if hasattr(widget, "valueChanged"):
                    widget.valueChanged.connect(self._schedule)
                elif isinstance(widget, QtWidgets.QComboBox):
                    widget.currentIndexChanged.connect(self._schedule)
            self._regime_toggled(self.regime_check.isChecked())

        def _load_controls(self) -> None:
            self.regime_check.setChecked(bool(self._score.get("enabled", True)))
            self.strategy_check.setChecked(bool(self._score.get("use_for_backtest", False)))
            for key, widget in self.inputs.items():
                value = self._score.get(key)
                if value is None:
                    continue
                if isinstance(widget, QtWidgets.QComboBox):
                    index = widget.findData(value)
                    if index >= 0:
                        widget.setCurrentIndex(index)
                elif isinstance(widget, QtWidgets.QCheckBox):
                    widget.setChecked(bool(value))
                elif isinstance(widget, QtWidgets.QSpinBox):
                    widget.setValue(int(value))
                else:
                    shown = (float(value) * 100.0 if key in {
                        "defensive_probe_fraction", "defensive_take_profit_pct"
                    } else float(value))
                    widget.setValue(shown)

        def _settings(self) -> Dict[str, Any]:
            result = dict(self._score)
            result["enabled"] = self.regime_check.isChecked()
            result["use_for_backtest"] = (
                self.regime_check.isChecked() and self.strategy_check.isChecked())
            for key, widget in self.inputs.items():
                if isinstance(widget, QtWidgets.QComboBox):
                    result[key] = widget.currentData()
                elif isinstance(widget, QtWidgets.QCheckBox):
                    result[key] = bool(widget.isChecked())
                else:
                    value = widget.value()
                    result[key] = (float(value) / 100.0 if key in {
                        "defensive_probe_fraction", "defensive_take_profit_pct"
                    } else value)
            return result

        def _schedule(self, _value: Any = None) -> None:
            if not self._building:
                self._debounce.start()

        def _regime_toggled(self, enabled: bool) -> None:
            self.chart.set_options(show_regime=enabled)
            self.condition_check.setEnabled(enabled)
            self.strategy_check.setEnabled(enabled)
            for widget in self.inputs.values():
                widget.setEnabled(enabled)
            self._schedule()

        def _change_interval(self, interval: str) -> None:
            if interval == self._requested_interval:
                return
            if interval == self._current_interval:
                # Clicking the still-visible interval cancels an outstanding
                # request for another interval.
                self._load_generation += 1
                self._requested_interval = self._current_interval
                self._interval_buttons[self._current_interval].setChecked(True)
                self._update_status()
                return
            raw_start, raw_end, all_period = period_provider()
            archive_start = pd.Timestamp("2011-08-19")
            archive_end = _timestamp(pd.Timestamp.now(tz="UTC").floor("D"))
            if all_period:
                period_start, period_end = archive_start, archive_end
            else:
                period_start, period_end = _period_bounds(raw_start, raw_end)
            period_span = max(period_end - period_start, pd.Timedelta(seconds=1))
            if interval in FULL_PERIOD_INTERVALS:
                # D/W/M 은 기간 전체를 한눈에 보는 용도이므로 백테스트 기간으로
                # 복원합니다.  분·시봉에서 돌아왔을 때도 마찬가지입니다.
                target = {"time": period_start + period_span / 2, "ratio": 0.5,
                          "span": period_span, "restore_period": True}
            else:
                # 분·시봉은 항상 한 화면 분량만 봅니다.  몇 년치 기간을 분봉으로
                # 통째로 읽으면 메모리가 바닥나고 창이 멈춥니다.
                # 보고 있던 지점을 그대로 기준 삼아 그 주변만 잘라 옵니다.
                current = self.chart.anchor_state()
                span = min(period_span, pd.Timedelta(
                    seconds=CHART_INTERVAL_SECONDS[interval]
                    * DEFAULT_VISIBLE_BARS[interval]))
                focus = current.get("time") if current else period_end
                ratio = float(current.get("ratio", 0.5)) if current else 1.0
                target = {"time": _timestamp(focus), "ratio": ratio, "span": span}
            self._request_interval(
                interval, target,
                full_period=bool(all_period and interval in FULL_PERIOD_INTERVALS))

        def _request_interval(self, interval: str,
                              anchor: Optional[Mapping[str, Any]] = None,
                              full_period: bool = False) -> None:
            start = end = visible_span = None
            if anchor:
                seconds = CHART_INTERVAL_SECONDS[interval]
                archive_start = pd.Timestamp("2011-08-19")
                archive_end = _timestamp(pd.Timestamp.now(tz="UTC").floor("D"))
                minimum_seconds = seconds * MIN_VISIBLE_BARS[interval]
                maximum_seconds = max(minimum_seconds, int(
                    (archive_end - archive_start).total_seconds()))
                raw_span = anchor.get("span")
                if raw_span is None:
                    raw_span = pd.Timedelta(
                        seconds=seconds * DEFAULT_VISIBLE_BARS[interval])
                raw_seconds = max(1.0, pd.Timedelta(raw_span).total_seconds())
                visible_span = pd.Timedelta(seconds=min(
                    maximum_seconds, max(minimum_seconds, raw_seconds)))
                ratio = float(np.clip(anchor.get("ratio", 0.5), 0.0, 1.0))
                focus = _timestamp(anchor["time"])
                visible_start = focus - visible_span * ratio
                visible_end = visible_start + visible_span
                pending = self._settings()
                warmup_bars = max(
                    420, int(pending.get("long_ma", 120)) + 10,
                    int(pending.get("macd_slow", 60))
                    + int(pending.get("macd_signal", 9)) + 190)
                margin = max(
                    visible_span,
                    pd.Timedelta(seconds=seconds * warmup_bars))
                start = max(archive_start, visible_start - margin)
                end = min(archive_end + pd.Timedelta(days=1),
                          visible_end + visible_span)
            self._requested_interval = interval
            label = dict(CHART_INTERVALS).get(interval, interval)
            self.status.setText(f"글로벌 BTC {label} 데이터 읽는 중…")
            self._load_generation += 1
            loader = _Loader(
                self._load_generation, interval, start=start, end=end,
                anchor=anchor, visible_span=visible_span,
                view_revision=self._view_revision, full_period=full_period)
            loader.finished.connect(self._data_loaded)
            self._loader = loader
            threading.Thread(target=loader.run, daemon=True).start()

        def _load_data(self, _checked: Any = False) -> None:
            raw_start, raw_end, all_period = period_provider()
            seconds = CHART_INTERVAL_SECONDS[self._current_interval]
            if all_period:
                start = pd.Timestamp("2011-08-19")
                end = _timestamp(pd.Timestamp.now(tz="UTC").floor("D"))
            else:
                start, end = _period_bounds(raw_start, raw_end)
            period_span = max(end - start, pd.Timedelta(seconds=seconds))
            if self._current_interval in FULL_PERIOD_INTERVALS:
                state = {"time": start + period_span / 2, "ratio": 0.5,
                         "span": period_span, "restore_period": True}
            else:
                # 분·시봉은 기간 전체를 담지 않습니다.  15년 아카이브의 한가운데를
                # 보여주는 것보다 가장 최근 구간을 오른쪽 끝에 붙여 여는 편이 낫습니다.
                span = min(period_span, pd.Timedelta(
                    seconds=seconds * DEFAULT_VISIBLE_BARS[self._current_interval]))
                state = {"time": end, "ratio": 1.0, "span": span}
            self._request_interval(
                self._current_interval, state,
                full_period=bool(all_period
                                 and self._current_interval in FULL_PERIOD_INTERVALS))

        def _data_loaded(self, payload: Dict[str, Any]) -> None:
            if int(payload.get("generation", -1)) != self._load_generation:
                return
            if not payload.get("ok"):
                if payload.get("needs_bootstrap"):
                    self._start_bootstrap()
                    return
                self.status.setText(f"데이터 오류: {payload.get('message', '알 수 없음')}")
                self._requested_interval = self._current_interval
                self._interval_buttons[self._current_interval].setChecked(True)
                return
            self._data = payload["data"].copy()
            if "timestamp" in self._data.columns:
                self._data.index = pd.DatetimeIndex(pd.to_datetime(
                    self._data["timestamp"], utc=True, errors="coerce"))
                self._data = self._data[~self._data.index.isna()]
            if self._data.empty:
                self.status.setText("글로벌 BTC 데이터가 없습니다")
                return
            self._current_interval = str(payload.get("interval", "1d"))
            self._requested_interval = self._current_interval
            self._loaded_full_period = bool(payload.get("full_period", False))
            self.chart.set_interval(self._current_interval)
            self._interval_buttons[self._current_interval].setChecked(True)
            for button in self._interval_buttons.values():
                button.setEnabled(True)
            self._update_status()
            self._recalculate()
            raw_start, raw_end, all_period = period_provider()
            if all_period:
                raw_start = pd.Timestamp("2011-08-19")
                raw_end = _timestamp(pd.Timestamp.now(tz="UTC").floor("D"))
            anchor = payload.get("anchor") or {}
            # D/W/M 로 돌아왔을 때는 기간을 그대로 복원합니다.  중심점+폭으로
            # 되돌리면 나눗셈 오차가 남아 경계가 1나노초씩 어긋납니다.
            restore_period = bool(anchor.get("restore_period"))
            self.chart.set_period(raw_start, raw_end, all_period,
                                  reset_view=restore_period)
            if not restore_period:
                visible_span = payload.get("visible_span") or anchor.get("span")
                if anchor.get("time") is not None and visible_span is not None:
                    self.chart.restore_anchor(anchor["time"], anchor.get("ratio", 0.5),
                                              pd.Timedelta(visible_span))
            self._update_period_label()

        def _update_status(self) -> None:
            if self._data.empty:
                return
            label = dict(CHART_INTERVALS).get(
                self._current_interval, self._current_interval)
            first, last = (_timestamp(self._data.index[0]),
                           _timestamp(self._data.index[-1]))
            time_format = "%Y-%m-%d" if self._current_interval in {
                "1d", "1w", "1mo"} else "%Y-%m-%d %H:%M"
            self.status.setText(
                f"{label} 로드 범위 {first.strftime(time_format)} ~ "
                f"{last.strftime(time_format)} · 모두 확정봉")

        def _recalculate(self) -> None:
            raw = self._settings()
            raw["decision_interval"] = self._current_interval
            errors = validate_scoring_config(raw, raw.get("use_for_backtest", False))
            if errors:
                self.summary.setText("설정 오류\n" + "\n".join(f"· {item}" for item in errors))
                return
            self._score = scoring_config({"regime_scoring": raw})
            self._config = dict(config_provider())
            self._config["regime_scoring"] = deepcopy(self._score)
            config_changed(deepcopy(self._score))
            self.chart.set_options(show_regime=self.regime_check.isChecked())
            if self._data.empty:
                return
            if int(self._score["long_ma"]) <= int(self._score["short_ma"]):
                self.summary.setText("장기 MA는 단기 MA보다 커야 합니다.")
                return
            if int(self._score["macd_slow"]) <= int(self._score["macd_fast"]):
                self.summary.setText("MACD 느림 기간은 빠름 기간보다 커야 합니다.")
                return
            diagnostic = build_regime_frame(self._data, self._config)
            self.chart.set_frames(self._data, diagnostic)
            raw_start, raw_end, all_period = period_provider()
            decision_data = self._data
            if not all_period:
                _analysis_start, analysis_end = _period_bounds(raw_start, raw_end)
                decision_index = pd.DatetimeIndex(
                    [_timestamp(value) for value in self._data.index])
                if hasattr(decision_index, "as_unit"):
                    decision_index = decision_index.as_unit("ns")
                right = int(decision_index.searchsorted(analysis_end, side="right"))
                decision_data = self._data.iloc[:right]
            latest = current_regime_decision(decision_data, self._config)
            if latest["regime"] == "판정 준비":
                self.summary.setText("선택한 설정으로 판정 가능한 데이터가 부족합니다.")
                return
            apply_text = ("켜짐(백테스트만)" if self._score["use_for_backtest"]
                          else "꺼짐(연구용)")
            detector_labels = dict(DETECTOR_OPTIONS)
            strategy_labels = dict(STRATEGY_OPTIONS)
            phase_key = ("bull_strategy" if latest["regime"] == "상승"
                         else "bear_strategy" if latest["regime"] == "하락"
                         else "stable_strategy")
            self.summary.setText(
                f"현재 {latest['regime']}\n"
                f"상승 {detector_labels[self._score['bull_detector']]} · "
                f"하락 {detector_labels[self._score['bear_detector']]}\n"
                f"현재 전략 {strategy_labels[self._score[phase_key]]}\n"
                f"전략 적용 {apply_text}")

        def _view_changed(self, start: Any, end: Any) -> None:
            self._view_revision += 1
            state = self.chart.anchor_state()
            if state is not None:
                self._view_states[self._current_interval] = dict(state)
            self._update_period_label()

        def _update_period_label(self) -> None:
            selected = self.chart.selected_period()
            backtest = self.chart.backtest_period()
            if all(selected) and all(backtest):
                screen_fmt = "%Y-%m-%d" if self._current_interval in {
                    "1d", "1w", "1mo"} else "%Y-%m-%d %H:%M"
                self.period_label.setText(
                    f"분석 {backtest[0].strftime('%Y-%m-%d')} ~ "
                    f"{backtest[1].strftime('%Y-%m-%d')}  ·  "
                    f"화면 {selected[0].strftime(screen_fmt)} ~ "
                    f"{selected[1].strftime(screen_fmt)}")

        def sync_period(self) -> None:
            if self._data.empty:
                return
            if self._requested_interval != self._current_interval:
                self._load_generation += 1
                self._requested_interval = self._current_interval
                self._interval_buttons[self._current_interval].setChecked(True)
            start, end, all_period = period_provider()
            if all_period:
                requested_start = pd.Timestamp("2011-08-19")
                requested_end = _timestamp(pd.Timestamp.now(tz="UTC").floor("D"))
            else:
                requested_start, requested_end = _period_bounds(start, end)
            period_span = max(requested_end - requested_start, pd.Timedelta(seconds=1))
            if self._current_interval not in FULL_PERIOD_INTERVALS:
                span = min(period_span, pd.Timedelta(
                    seconds=CHART_INTERVAL_SECONDS[self._current_interval]
                    * DEFAULT_VISIBLE_BARS[self._current_interval]))
                self._request_interval(self._current_interval, {
                    "time": requested_end, "ratio": 1.0, "span": span,
                }, full_period=False)
                return
            first, last = (_timestamp(self._data.index[0]),
                           _timestamp(self._data.index[-1]))
            if (requested_start < first or requested_end > last
                    or (all_period and not self._loaded_full_period)):
                self._request_interval(self._current_interval, {
                    "time": requested_start + period_span / 2, "ratio": 0.5,
                    "span": period_span, "restore_period": True,
                }, full_period=all_period)
                return
            self.chart.set_period(requested_start, requested_end, all_period)
            self._update_period_label()

        def _start_bootstrap(self) -> None:
            """정본이 없으면 안내문 대신 직접 받아 옵니다."""
            if self._bootstrap_thread is not None:
                return
            from global_market_data import missing_requirements
            missing = missing_requirements()
            if missing:
                # 14년치를 다 받은 뒤에 저장 단계에서 막히면 받은 것이 전부
                # 버려집니다. 시작하기 전에 멈춥니다.
                names = " ".join(missing)
                self.status.setText(f"설치 필요: pip install {names}")
                self.bootstrap_row.setVisible(True)
                self.bootstrap_bar.setRange(0, 1)
                self.bootstrap_bar.setValue(0)
                self.bootstrap_bar.setFormat("수집 불가")
                self.bootstrap_label.setText(
                    f"시세 저장에 {names} 가 필요합니다 · pip install {names}")
                self.bootstrap_cancel.setText("닫기")
                self.bootstrap_cancel.setEnabled(True)
                return
            self.bootstrap_cancel.setText("중단")
            self.status.setText("BTC 정본 최초 수집 중…")
            self.bootstrap_label.setText("Bitstamp BTC/USD 1분봉 내려받는 중…")
            self.bootstrap_bar.setRange(0, 0)
            self.bootstrap_cancel.setEnabled(True)
            self.bootstrap_row.setVisible(True)
            for button in self._interval_buttons.values():
                button.setEnabled(False)
            worker = _BootstrapWorker()
            worker.progress.connect(self._bootstrap_progress)
            worker.finished.connect(self._bootstrap_finished)
            self._bootstrap_worker = worker
            self._bootstrap_thread = threading.Thread(
                target=worker.run, daemon=True)
            self._bootstrap_thread.start()

        def _cancel_bootstrap(self) -> None:
            if self._bootstrap_worker is None:
                # 아직 시작도 못 한 상태(패키지 부족)면 안내만 접습니다.
                self.bootstrap_row.setVisible(False)
                return
            self._bootstrap_worker.cancel()
            self.bootstrap_cancel.setEnabled(False)
            self.bootstrap_label.setText("중단하는 중… 받던 달까지는 저장됩니다")

        def _bootstrap_progress(self, state: Any) -> None:
            done, total = state.get("done"), state.get("total")
            phase = {"1m": "1분봉 수집", "1h": "시간봉 집계",
                     "1d": "일봉 집계"}.get(state.get("interval", ""), "처리")
            if done is not None and total:
                self.bootstrap_bar.setRange(0, int(total))
                self.bootstrap_bar.setValue(int(done))
                self.bootstrap_bar.setFormat(f"%v / %m  ({phase})")
                self.bootstrap_label.setText(f"{phase} · {done}/{total}개월")
            else:
                self.bootstrap_bar.setRange(0, 0)
                self.bootstrap_label.setText(f"{phase}…")

        def _bootstrap_finished(self, payload: Any) -> None:
            self._bootstrap_thread = None
            self._bootstrap_worker = None
            self.bootstrap_row.setVisible(False)
            for button in self._interval_buttons.values():
                button.setEnabled(True)
            if payload.get("ok"):
                self.status.setText("BTC 정본 수집 완료 · 다시 읽는 중…")
                self._load_data()
                return
            if payload.get("cancelled"):
                self.status.setText(
                    "수집을 중단했습니다. 새로고침하면 남은 구간부터 이어 받습니다.")
                return
            self.status.setText(f"정본 수집 실패: {payload.get('message', '알 수 없음')}")

        def _align_field_widths(self, *forms: Any) -> None:
            """폼들의 입력 칸 폭을 가장 넓은 하나에 맞춥니다."""
            fields = []
            for form in forms:
                for row in range(form.rowCount()):
                    item = form.itemAt(
                        row, QtWidgets.QFormLayout.ItemRole.FieldRole)
                    widget = item.widget() if item is not None else None
                    if widget is not None:
                        fields.append(widget)
            if not fields:
                return
            width = min(FIELD_WIDTH_CAP,
                        max(widget.sizeHint().width() for widget in fields))
            for widget in fields:
                widget.setFixedWidth(width)

        def _request_visible_window(self, start: Any, end: Any) -> None:
            """Slide the bounded archive window after a pan/zoom reaches an edge."""
            if self._current_interval in FULL_PERIOD_INTERVALS:
                return
            start_ts, end_ts = _timestamp(start), _timestamp(end)
            if start_ts >= end_ts:
                return
            span = end_ts - start_ts
            self._request_interval(self._current_interval, {
                "time": start_ts + span / 2,
                "ratio": 0.5, "span": span,
            }, full_period=False)

        def _apply_period(self) -> None:
            start, end = self.chart.selected_period()
            if start is not None and end is not None and start <= end:
                period_apply(start, end)
            else:
                self.summary.setText("적용할 차트 기간이 올바르지 않습니다.")

    return RegimeChartWindow()
