"""Tray icon rendering with mouse SVG and in-shape battery fill."""

from __future__ import annotations

import os
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
MOUSE_SVG = os.path.join(ASSETS_DIR, "mouse-2.svg")
MOUSE_SOLID_SVG = os.path.join(ASSETS_DIR, "mouse-solid.svg")

CHARGING_COLOR = "#89b4fa"
OUTLINE_WIDTH_RATIO = 0.08
LEVEL_BUCKET = 5


@dataclass
class _StaticLayers:
    width: int
    height: int
    offset_x: int
    offset_y: int
    interior_clip: QPixmap
    ring: QPixmap


class MouseBatteryIconRenderer:
    CANVAS_SIZE = 64

    def __init__(
        self,
        fill_color: str = "#a6e3a1",
        border_color: str = "#a6adc8",
        charging_color: str = CHARGING_COLOR,
        scale: int = 100,
        show_text: bool = False,
    ) -> None:
        self.fill_color = fill_color
        self.border_color = border_color
        self.charging_color = charging_color
        self.scale = max(50, min(150, scale))
        self.show_text = show_text
        self._outline_svg = QSvgRenderer(MOUSE_SVG) if os.path.exists(MOUSE_SVG) else None
        self._fill_svg = (
            QSvgRenderer(MOUSE_SOLID_SVG) if os.path.exists(MOUSE_SOLID_SVG) else self._outline_svg
        )
        self._icon_cache: dict[tuple, QIcon] = {}
        self._static: _StaticLayers | None = None
        self._rebuild_static_layers()

    def reconfigure(
        self,
        *,
        fill_color: str | None = None,
        border_color: str | None = None,
        charging_color: str | None = None,
        scale: int | None = None,
        show_text: bool | None = None,
    ) -> None:
        if fill_color is not None:
            self.fill_color = fill_color
        if border_color is not None:
            self.border_color = border_color
        if charging_color is not None:
            self.charging_color = charging_color
        if scale is not None:
            self.scale = max(50, min(150, scale))
        if show_text is not None:
            self.show_text = show_text
        self._rebuild_static_layers()

    def _bucket_level(self, percentage: int) -> int:
        if LEVEL_BUCKET <= 1:
            return max(0, min(100, percentage))
        stepped = round(percentage / LEVEL_BUCKET) * LEVEL_BUCKET
        return max(0, min(100, int(stepped)))

    def _cache_key(
        self, percentage: int, is_charging: bool, error: bool, asleep: bool = False
    ) -> tuple:
        if error:
            return ("error",)
        if asleep:
            return ("asleep", self._bucket_level(percentage))
        return ("ok", self._bucket_level(percentage), is_charging)

    def render(
        self,
        percentage: int,
        is_charging: bool,
        error: bool = False,
        asleep: bool = False,
    ) -> QIcon:
        key = self._cache_key(percentage, is_charging, error, asleep)
        cached = self._icon_cache.get(key)
        if cached is not None:
            return cached

        icon = self._render_uncached(percentage, is_charging, error, asleep)
        self._icon_cache[key] = icon
        return icon

    def _mouse_rect(self, canvas: int) -> QRectF:
        raw_size = int(canvas * (self.scale / 100.0))
        size = raw_size - (raw_size % 2)
        offset = (canvas - size) / 2.0
        return QRectF(offset, offset, size, size)

    def _rebuild_static_layers(self) -> None:
        self._icon_cache.clear()
        canvas = self.CANVAS_SIZE
        rect = self._mouse_rect(canvas)
        w = int(rect.width())
        h = int(rect.height())
        ox = int(rect.x())
        oy = int(rect.y())

        if not self._fill_svg or not self._outline_svg:
            self._static = None
            return

        interior = QPixmap(w, h)
        interior.fill(Qt.transparent)
        p_int = QPainter(interior)
        self._fill_svg.render(p_int, QRectF(0, 0, w, h))
        p_int.end()

        ring = QPixmap(w, h)
        ring.fill(Qt.transparent)
        p_ring = QPainter(ring)
        self._outline_svg.render(p_ring, QRectF(0, 0, w, h))
        p_ring.setCompositionMode(QPainter.CompositionMode_SourceIn)
        p_ring.fillRect(0, 0, w, h, QColor(self.border_color))
        p_ring.end()

        self._static = _StaticLayers(
            width=w,
            height=h,
            offset_x=ox,
            offset_y=oy,
            interior_clip=interior,
            ring=ring,
        )

    def _render_uncached(
        self,
        percentage: int,
        is_charging: bool,
        error: bool = False,
        asleep: bool = False,
    ) -> QIcon:
        canvas = self.CANVAS_SIZE
        pixmap = QPixmap(canvas, canvas)
        pixmap.fill(Qt.transparent)

        if not self._static:
            return QIcon(pixmap)

        st = self._static
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.Antialiasing)

        if error:
            dim_ring = QPixmap(st.ring)
            dp = QPainter(dim_ring)
            dp.setCompositionMode(QPainter.CompositionMode_SourceIn)
            dp.fillRect(0, 0, st.width, st.height, QColor(128, 128, 128, 160))
            dp.end()
            p.drawPixmap(st.offset_x, st.offset_y, dim_ring)
            p.end()
            return QIcon(pixmap)

        level = self._bucket_level(percentage)
        fill_height = int(st.height * (level / 100.0))

        fill_pix = QPixmap(st.width, st.height)
        fill_pix.fill(Qt.transparent)
        fp = QPainter(fill_pix)

        if fill_height > 0:
            color = QColor(self.charging_color if is_charging else self.fill_color)
            if asleep:
                color.setAlpha(160)
            fp.fillRect(0, st.height - fill_height, st.width, fill_height, color)

        fp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        fp.drawPixmap(0, 0, st.interior_clip)
        fp.end()

        p.drawPixmap(st.offset_x, st.offset_y, fill_pix)

        ring_to_draw = st.ring
        if asleep:
            dim_ring = QPixmap(st.ring)
            dp = QPainter(dim_ring)
            dp.setCompositionMode(QPainter.CompositionMode_SourceIn)
            dp.fillRect(0, 0, st.width, st.height, QColor(160, 160, 175, 180))
            dp.end()
            ring_to_draw = dim_ring

        p.drawPixmap(st.offset_x, st.offset_y, ring_to_draw)

        if self.show_text and level >= 0 and not is_charging:
            p.setPen(QColor(self.border_color))
            font = QFont()
            font.setPixelSize(max(8, int(st.height * 0.28)))
            font.setBold(True)
            p.setFont(font)
            text = f"{level}"
            p.drawText(
                QRectF(st.offset_x, st.offset_y, st.width, st.height),
                Qt.AlignCenter,
                text,
            )

        p.end()
        return QIcon(pixmap)

