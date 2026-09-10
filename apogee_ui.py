#!/usr/bin/env python3

"""Modern control panel for the Apogee Duet (USB).

The USB protocol and the device state live in ``take_control``; this module
owns presentation only.

Everything is painted through ``wx.GraphicsContext`` instead of native GTK
widgets: the panel then looks the same on any desktop theme and the faders can
follow a drag with a frame of latency.

Two device facts drive the design:

* A USB control transfer to the Duet costs ~3 ms while a mouse drag emits
  hundreds of events per second, so :class:`ThrottledWriter` coalesces live
  drags into a bounded write rate and applies the final value immediately.
* The two line input types bypass the preamp, so they expose no gain register
  and the gain fader is rendered inert for them.
"""

import math
from functools import lru_cache

import wx

from take_control import (
  ApogeeDuet,
  ChannelType,
  InputType,
  OutputSource,
  OutputType,
  SpeakerOutputType,
  SoftwareReturnSource,
)

#
# Theme
#

BG = wx.Colour(0x0D, 0x10, 0x15)
NAV_BG = wx.Colour(0x11, 0x14, 0x1A)
NAV_ACTIVE = wx.Colour(0x1D, 0x24, 0x33)
NAV_HOVER = wx.Colour(0x17, 0x1D, 0x28)
CARD = wx.Colour(0x17, 0x1C, 0x25)
CARD_BORDER = wx.Colour(0x23, 0x2A, 0x37)
TRACK = wx.Colour(0x2E, 0x36, 0x46)
TRACK_HOVER = wx.Colour(0x3A, 0x44, 0x57)
ACCENT = wx.Colour(0xFF, 0x9E, 0x4A)
ACCENT_DEEP = wx.Colour(0xE0, 0x6B, 0x28)
TEXT = wx.Colour(0xE8, 0xEB, 0xF2)
TEXT_DIM = wx.Colour(0x92, 0x9B, 0xAF)
TEXT_SOFT = wx.Colour(0xA9, 0xB1, 0xC4)
TEXT_FAINT = wx.Colour(0x7B, 0x84, 0x9A)
OK = wx.Colour(0x4E, 0xD0, 0x8A)
WARN = wx.Colour(0xF2, 0xC1, 0x4B)
DANGER = wx.Colour(0xE8, 0x4C, 0x4C)
INK = wx.Colour(0x14, 0x17, 0x1D)

SEMIBOLD = getattr(wx, 'FONTWEIGHT_SEMIBOLD', wx.FONTWEIGHT_BOLD)

_UI_FACES = ('Adwaita Sans', 'Inter', 'Cantarell', 'Noto Sans', 'DejaVu Sans')
_MONO_FACES = ('JetBrainsMono NF', 'Adwaita Mono', 'DejaVu Sans Mono')
_FACES = None


def _first_available(candidates, available):
  for face in candidates:
    if face in available:
      return face
  return candidates[-1]


def init_theme():
  """Resolve the font families, which requires a live wx.App."""
  global _FACES
  if _FACES is None:
    enumerator = wx.FontEnumerator()
    enumerator.EnumerateFacenames()
    available = set(enumerator.GetFacenames())
    _FACES = {
      'ui': _first_available(_UI_FACES, available),
      'mono': _first_available(_MONO_FACES, available),
    }
  return _FACES


@lru_cache(maxsize=None)
def font(size=10, weight=None, mono=False):
  info = wx.FontInfo(size).FaceName(init_theme()['mono' if mono else 'ui'])
  if weight is not None:
    info = info.Weight(weight)
  return wx.Font(info)


#
# Device writes
#

class ThrottledWriter(object):
  """Coalesces rapid control changes into a bounded device write rate.

  Live drags call :meth:`submit` on every value change; the pending write for a
  target is replaced (never queued twice) and flushed by a one shot timer. The
  final value of a drag is written straight away through ``immediate`` so the
  device always ends up on the value the user released on.
  """

  def __init__(self, owner, interval_ms=60):
    self._pending = {}
    self._interval = interval_ms
    self._timer = wx.Timer(owner)
    owner.Bind(wx.EVT_TIMER, self._on_timer, self._timer)

  def submit(self, target, apply, immediate=False):
    if immediate:
      self._pending.pop(target, None)
      apply()
      return
    self._pending[target] = apply
    if not self._timer.IsRunning():
      self._timer.Start(self._interval, oneShot=True)

  def flush(self):
    self._timer.Stop()
    if not self._pending:
      return
    pending, self._pending = self._pending, {}
    for apply in pending.values():
      apply()

  def _on_timer(self, event):
    self.flush()


#
# Painting helpers
#

def draw_text(gc, text, x, y, colour, face, align='left'):
  """Draw ``text`` with its left edge, centre or right edge at ``x``."""
  gc.SetFont(face, colour)
  width = gc.GetTextExtent(text)[0]
  if align == 'center':
    x -= width / 2.0
  elif align == 'right':
    x -= width
  gc.DrawText(text, int(round(x)), int(round(y)))


def draw_tracked_text(gc, text, x, y, colour, face, tracking=1, align='left'):
  """Uppercase label style: letter spaced, as no GC API exposes tracking."""
  gc.SetFont(face, colour)
  widths = [gc.GetTextExtent(char)[0] for char in text]
  total = sum(widths) + tracking * max(0, len(text) - 1)
  if align == 'center':
    x -= total / 2.0
  elif align == 'right':
    x -= total
  x = int(round(x))
  for char, width in zip(text, widths):
    gc.DrawText(char, x, y)
    x += width + tracking


def text_height(gc, text, face):
  gc.SetFont(face, TEXT)
  return gc.GetTextExtent(text)[1]


def draw_circle(gc, x, y, radius):
  """GraphicsContext only exposes ellipse drawing."""
  gc.DrawEllipse(x - radius, y - radius, 2 * radius, 2 * radius)


class Painted(wx.Control):
  """Owner drawn control: no native widget, so no theme interference."""

  def __init__(self, parent, surface, size=None):
    wx.Control.__init__(self, parent, style=wx.BORDER_NONE | wx.WANTS_CHARS)
    self._surface = surface
    self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
    if size is not None:
      self.SetMinSize(size)
    self.Bind(wx.EVT_PAINT, self._on_paint)
    self.Bind(wx.EVT_SIZE, self._on_size)

  def _on_size(self, event):
    self.Refresh()
    event.Skip()

  def _on_paint(self, event):
    dc = wx.BufferedPaintDC(self)
    dc.SetBackground(wx.Brush(self._surface))
    dc.Clear()
    gc = wx.GraphicsContext.Create(dc)
    if gc is not None:
      self.paint(gc, self.GetSize())

  def paint(self, gc, size):
    raise NotImplementedError


#
# Widgets
#

class Fader(Painted):
  """Vertical level fader; ``on_change(value, final)`` reports every change."""

  TRACK_WIDTH = 8
  THUMB_WIDTH = 26
  THUMB_HEIGHT = 12

  def __init__(self, parent, surface, low, high, value, on_change, height=176):
    size = wx.Size(parent.FromDIP(self.THUMB_WIDTH + 22), parent.FromDIP(height))
    Painted.__init__(self, parent, surface, size)
    self._low = low
    self._high = high
    self._value = value
    self._on_change = on_change
    self._interactive = True
    self._dragging = False
    self._hover = False
    self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
    self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
    self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
    self.Bind(wx.EVT_MOTION, self._on_motion)
    self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)
    self.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
    self.Bind(wx.EVT_KEY_DOWN, self._on_key)
    self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
    self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)

  def get_value(self):
    return self._value

  def set_value(self, value, notify=False, final=True):
    value = max(self._low, min(self._high, int(value)))
    changed = value != self._value
    self._value = value
    if changed:
      self.Refresh()
    if changed and notify:
      self._on_change(value, final)

  def set_range(self, low, high):
    self._low = low
    self._high = high
    self.set_value(self._value)

  def set_interactive(self, interactive):
    self._interactive = interactive
    self.SetCursor(wx.Cursor(wx.CURSOR_HAND if interactive else wx.CURSOR_ARROW))
    self.Refresh()

  def _bounds(self, size):
    margin = self.FromDIP(12)
    return margin, size.height - margin

  def _ratio(self):
    span = self._high - self._low
    return 0.0 if span <= 0 else (self._value - self._low) / float(span)

  def _y_for(self, ratio, top, bottom):
    return bottom - ratio * (bottom - top)

  def _value_at(self, y, size):
    top, bottom = self._bounds(size)
    if bottom <= top:
      return self._low
    ratio = min(1.0, max(0.0, (bottom - y) / float(bottom - top)))
    return int(round(self._low + ratio * (self._high - self._low)))

  def paint(self, gc, size):
    top, bottom = self._bounds(size)
    track_width = self.FromDIP(self.TRACK_WIDTH)
    radius = track_width / 2.0
    x = (size.width - track_width) / 2.0
    gc.SetPen(wx.TRANSPARENT_PEN)
    gc.SetBrush(wx.Brush(TRACK if self._interactive else wx.Colour(0x1F, 0x24, 0x2E)))
    gc.DrawRoundedRectangle(x, top, track_width, bottom - top, radius)

    y = self._y_for(self._ratio(), top, bottom)
    if self._interactive and bottom - y > 1:
      gc.SetBrush(gc.CreateLinearGradientBrush(x, bottom, x, y, ACCENT_DEEP, ACCENT))
      gc.DrawRoundedRectangle(x, y, track_width, bottom - y, min(radius, (bottom - y) / 2.0))

    thumb_width = self.FromDIP(self.THUMB_WIDTH)
    thumb_height = self.FromDIP(self.THUMB_HEIGHT)
    thumb_x = (size.width - thumb_width) / 2.0
    thumb_y = y - thumb_height / 2.0
    gc.SetPen(wx.Pen(wx.Colour(0x08, 0x0A, 0x0E), 1))
    if not self._interactive:
      body = wx.Colour(0x62, 0x6B, 0x7D)
    elif self._hover or self._dragging:
      body = wx.Colour(0xFF, 0xFF, 0xFF)
    else:
      body = wx.Colour(0xDD, 0xE3, 0xEE)
    gc.SetBrush(wx.Brush(body))
    gc.DrawRoundedRectangle(thumb_x, thumb_y, thumb_width, thumb_height, thumb_height / 2.0)

    mark = self.FromDIP(6)
    gc.SetPen(wx.Pen(INK if self._interactive else wx.Colour(0x14, 0x17, 0x1D), self.FromDIP(2)))
    gc.StrokeLine(thumb_x + mark, y, thumb_x + thumb_width - mark, y)

  def _on_enter(self, event):
    self._hover = True
    self.Refresh()

  def _on_leave(self, event):
    self._hover = False
    self.Refresh()

  def _on_left_down(self, event):
    if not self._interactive:
      return
    self.SetFocus()
    self._dragging = True
    self.set_value(self._value_at(event.GetY(), self.GetSize()), notify=True, final=False)
    if not self.HasCapture():
      self.CaptureMouse()

  def _on_motion(self, event):
    if self._dragging and event.LeftIsDown():
      self.set_value(self._value_at(event.GetY(), self.GetSize()), notify=True, final=False)

  def _on_left_up(self, event):
    if not self._dragging:
      return
    self._dragging = False
    if self.HasCapture():
      self.ReleaseMouse()
    self.set_value(self._value_at(event.GetY(), self.GetSize()), notify=True, final=True)

  def _on_capture_lost(self, event):
    self._dragging = False

  def _on_wheel(self, event):
    if not self._interactive:
      return
    step = 5 if event.ShiftDown() or event.ControlDown() else 1
    direction = 1 if event.GetWheelRotation() > 0 else -1
    self.set_value(self._value + direction * step, notify=True)

  def _on_key(self, event):
    if not self._interactive:
      return
    code = event.GetKeyCode()
    span = self._high - self._low
    if code in (wx.WXK_UP, wx.WXK_RIGHT):
      self.set_value(self._value + 1, notify=True)
    elif code in (wx.WXK_DOWN, wx.WXK_LEFT):
      self.set_value(self._value - 1, notify=True)
    elif code == wx.WXK_PAGEUP:
      self.set_value(self._value + max(1, span // 10), notify=True)
    elif code == wx.WXK_PAGEDOWN:
      self.set_value(self._value - max(1, span // 10), notify=True)
    elif code == wx.WXK_HOME:
      self.set_value(self._low, notify=True)
    elif code == wx.WXK_END:
      self.set_value(self._high, notify=True)
    else:
      event.Skip()


class Knob(Painted):
  """Rotary control for the mixer pan; drag vertically, double click centres."""

  DOTS = 21
  SWEEP = 135.0

  def __init__(self, parent, surface, low, high, value, on_change, center=0, diameter=78):
    size = wx.Size(parent.FromDIP(diameter), parent.FromDIP(diameter))
    Painted.__init__(self, parent, surface, size)
    self._low = low
    self._high = high
    self._value = value
    self._center = center
    self._on_change = on_change
    self._drag_origin = None
    self._hover = False
    self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
    self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
    self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
    self.Bind(wx.EVT_MOTION, self._on_motion)
    self.Bind(wx.EVT_LEFT_DCLICK, self._on_double_click)
    self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)
    self.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
    self.Bind(wx.EVT_KEY_DOWN, self._on_key)
    self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
    self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)

  def get_value(self):
    return self._value

  def set_value(self, value, notify=False):
    value = max(self._low, min(self._high, int(round(value))))
    changed = value != self._value
    self._value = value
    if changed:
      self.Refresh()
    if changed and notify:
      self._on_change(value)

  def _ratio(self, value):
    span = self._high - self._low
    return 0.0 if span <= 0 else (value - self._low) / float(span)

  def _angle(self, value):
    return math.radians(90.0 + self.SWEEP - 2.0 * self.SWEEP * self._ratio(value))

  def _point(self, cx, cy, radius, value):
    angle = self._angle(value)
    return cx + radius * math.cos(angle), cy - radius * math.sin(angle)

  def paint(self, gc, size):
    cx = size.width / 2.0
    cy = size.height / 2.0
    radius = min(size.width, size.height) / 2.0
    ring = radius - self.FromDIP(5)
    value_angle = self._angle(self._value)

    dot = self.FromDIP(2)
    for index in range(self.DOTS):
      value = self._low + (self._high - self._low) * index / float(self.DOTS - 1)
      x, y = self._point(cx, cy, ring, value)
      active = self._angle(value) >= value_angle - 1e-6
      gc.SetPen(wx.TRANSPARENT_PEN)
      gc.SetBrush(wx.Brush(ACCENT if active else TRACK))
      draw_circle(gc, x, y, dot)

    body = radius - self.FromDIP(12)
    gc.SetPen(wx.Pen(wx.Colour(0x08, 0x0A, 0x0E), 1))
    gc.SetBrush(wx.Brush(wx.Colour(0x2A, 0x32, 0x41) if self._hover else wx.Colour(0x23, 0x2A, 0x38)))
    draw_circle(gc, cx, cy, body)

    x0, y0 = self._point(cx, cy, body * 0.35, self._value)
    x1, y1 = self._point(cx, cy, body * 0.82, self._value)
    gc.SetPen(wx.Pen(ACCENT, self.FromDIP(3)))
    gc.StrokeLine(x0, y0, x1, y1)

  def _on_enter(self, event):
    self._hover = True
    self.Refresh()

  def _on_leave(self, event):
    self._hover = False
    self.Refresh()

  def _on_left_down(self, event):
    self.SetFocus()
    self._drag_origin = (event.GetY(), self._value)
    if not self.HasCapture():
      self.CaptureMouse()

  def _on_motion(self, event):
    if self._drag_origin is None or not event.LeftIsDown():
      return
    origin_y, origin_value = self._drag_origin
    pixels = self.FromDIP(150)
    span = self._high - self._low
    self.set_value(origin_value + (origin_y - event.GetY()) * span / float(pixels), notify=True)

  def _on_left_up(self, event):
    if self._drag_origin is None:
      return
    self._drag_origin = None
    if self.HasCapture():
      self.ReleaseMouse()

  def _on_double_click(self, event):
    self.set_value(self._center, notify=True)

  def _on_capture_lost(self, event):
    self._drag_origin = None

  def _on_wheel(self, event):
    step = 4 if event.ShiftDown() or event.ControlDown() else 1
    direction = 1 if event.GetWheelRotation() > 0 else -1
    self.set_value(self._value + direction * step, notify=True)

  def _on_key(self, event):
    code = event.GetKeyCode()
    if code in (wx.WXK_UP, wx.WXK_RIGHT):
      self.set_value(self._value + 1, notify=True)
    elif code in (wx.WXK_DOWN, wx.WXK_LEFT):
      self.set_value(self._value - 1, notify=True)
    elif code == wx.WXK_HOME:
      self.set_value(self._low, notify=True)
    elif code == wx.WXK_END:
      self.set_value(self._high, notify=True)
    else:
      event.Skip()


class Segmented(Painted):
  """Segmented selector replacing the native choice control.

  ``columns`` lays the segments out as a grid; a single row is the default.
  """

  PADDING = 3

  def __init__(self, parent, surface, labels, index, on_change, columns=None, cell_height=30):
    Painted.__init__(self, parent, surface)
    self._labels = list(labels)
    self._index = index
    self._on_change = on_change
    self._columns = columns or len(self._labels)
    self._rows = int(math.ceil(len(self._labels) / float(self._columns)))
    self._cell_height = cell_height
    self._hover = None
    self.SetMinSize(wx.Size(parent.FromDIP(120), parent.FromDIP(cell_height * self._rows + 2 * self.PADDING)))
    self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
    self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
    self.Bind(wx.EVT_MOTION, self._on_motion)
    self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
    self.Bind(wx.EVT_KEY_DOWN, self._on_key)

  def get_selection(self):
    return self._index

  def set_selection(self, index):
    self._index = index
    self.Refresh()

  def _geometry(self, size):
    pad = self.FromDIP(self.PADDING)
    cell_width = (size.width - 2 * pad) / float(self._columns)
    cell_height = (size.height - 2 * pad) / float(self._rows)
    return pad, cell_width, cell_height

  def _index_at(self, x, y, size):
    pad, cell_width, cell_height = self._geometry(size)
    column = int((x - pad) // cell_width)
    row = int((y - pad) // cell_height)
    if column < 0 or column >= self._columns or row < 0 or row >= self._rows:
      return None
    index = row * self._columns + column
    return index if 0 <= index < len(self._labels) else None

  def paint(self, gc, size):
    pad, cell_width, cell_height = self._geometry(size)
    gc.SetPen(wx.TRANSPARENT_PEN)
    gc.SetBrush(wx.Brush(wx.Colour(0x1E, 0x24, 0x30)))
    gc.DrawRoundedRectangle(0, 0, size.width, size.height, self.FromDIP(9))

    face = font(9, SEMIBOLD)
    for index, label in enumerate(self._labels):
      row, column = divmod(index, self._columns)
      x = pad + column * cell_width
      y = pad + row * cell_height
      selected = index == self._index
      if selected:
        gc.SetBrush(wx.Brush(ACCENT))
        gc.DrawRoundedRectangle(x + 1, y + 1, cell_width - 2, cell_height - 2, self.FromDIP(7))
      elif index == self._hover:
        gc.SetBrush(wx.Brush(TRACK_HOVER))
        gc.DrawRoundedRectangle(x + 1, y + 1, cell_width - 2, cell_height - 2, self.FromDIP(7))
      height = text_height(gc, label, face)
      draw_text(gc, label, x + cell_width / 2.0, y + (cell_height - height) / 2.0,
                INK if selected else TEXT_SOFT, face, align='center')

  def _on_motion(self, event):
    index = self._index_at(event.GetX(), event.GetY(), self.GetSize())
    if index != self._hover:
      self._hover = index
      self.Refresh()

  def _on_leave(self, event):
    self._hover = None
    self.Refresh()

  def _on_left_down(self, event):
    self.SetFocus()
    index = self._index_at(event.GetX(), event.GetY(), self.GetSize())
    if index is not None and index != self._index:
      self._index = index
      self.Refresh()
      self._on_change(index)

  def _on_key(self, event):
    code = event.GetKeyCode()
    index = self._index
    if code in (wx.WXK_RIGHT, wx.WXK_DOWN):
      index += 1
    elif code in (wx.WXK_LEFT, wx.WXK_UP):
      index -= 1
    else:
      event.Skip()
      return
    if 0 <= index < len(self._labels) and index != self._index:
      self._index = index
      self.Refresh()
      self._on_change(index)


class SwitchRow(Painted):
  """Label plus a pill switch; the whole row is the hit target."""

  SWITCH_WIDTH = 38
  SWITCH_HEIGHT = 21

  def __init__(self, parent, surface, label, value, on_toggle, active_colour=ACCENT,
               enabled=True, height=32):
    Painted.__init__(self, parent, surface, wx.Size(parent.FromDIP(150), parent.FromDIP(height)))
    self._label = label
    self._value = bool(value)
    self._on_toggle = on_toggle
    self._active_colour = active_colour
    self._enabled = enabled
    self._hover = False
    self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
    self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
    self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
    self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)

  def get_value(self):
    return self._value

  def set_value(self, value):
    value = bool(value)
    if value != self._value:
      self._value = value
      self.Refresh()

  def set_interactive(self, interactive):
    self._enabled = interactive
    self.SetCursor(wx.Cursor(wx.CURSOR_HAND if interactive else wx.CURSOR_ARROW))
    self.Refresh()

  def paint(self, gc, size):
    if self._enabled:
      label_colour = TEXT if self._hover else wx.Colour(0xD2, 0xD7, 0xE2)
    else:
      label_colour = TEXT_FAINT
    draw_text(gc, self._label, 0, (size.height - self.FromDIP(15)) / 2.0, label_colour, font(10))

    width = self.FromDIP(self.SWITCH_WIDTH)
    height = self.FromDIP(self.SWITCH_HEIGHT)
    x = size.width - width
    y = (size.height - height) / 2.0
    gc.SetPen(wx.TRANSPARENT_PEN)
    if self._value:
      track = self._active_colour if self._enabled else wx.Colour(0x5A, 0x4A, 0x38)
    elif not self._enabled:
      track = wx.Colour(0x23, 0x29, 0x35)
    else:
      track = TRACK_HOVER if self._hover else TRACK
    gc.SetBrush(wx.Brush(track))
    gc.DrawRoundedRectangle(x, y, width, height, height / 2.0)

    knob = height - self.FromDIP(5)
    inset = self.FromDIP(3)
    knob_x = x + width - knob - inset if self._value else x + inset
    if self._value:
      knob_colour = wx.Colour(0xFF, 0xFF, 0xFF) if self._enabled else wx.Colour(0xA8, 0x9B, 0x86)
    else:
      knob_colour = wx.Colour(0x8B, 0x94, 0xA8) if self._enabled else wx.Colour(0x5A, 0x64, 0x73)
    gc.SetBrush(wx.Brush(knob_colour))
    draw_circle(gc, knob_x + knob / 2.0, y + height / 2.0, knob / 2.0)

  def _on_enter(self, event):
    self._hover = True
    self.Refresh()

  def _on_leave(self, event):
    self._hover = False
    self.Refresh()

  def _on_left_down(self, event):
    if not self._enabled:
      return
    self._value = not self._value
    self.Refresh()
    self._on_toggle(self._value)


class ChipButton(Painted):
  """Compact toggle used for the mixer mute and solo buttons."""

  def __init__(self, parent, surface, label, active_colour, on_toggle, width=48, height=27):
    Painted.__init__(self, parent, surface, wx.Size(parent.FromDIP(width), parent.FromDIP(height)))
    self._label = label
    self._active_colour = active_colour
    self._on_toggle = on_toggle
    self._value = False
    self._hover = False
    self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
    self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
    self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
    self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)

  def get_value(self):
    return self._value

  def set_value(self, value):
    value = bool(value)
    if value != self._value:
      self._value = value
      self.Refresh()

  def paint(self, gc, size):
    if self._value:
      background = self._active_colour
    else:
      background = TRACK_HOVER if self._hover else TRACK
    gc.SetPen(wx.TRANSPARENT_PEN)
    gc.SetBrush(wx.Brush(background))
    gc.DrawRoundedRectangle(0, 0, size.width, size.height, self.FromDIP(7))
    face = font(9, SEMIBOLD)
    height = text_height(gc, self._label, face)
    draw_text(gc, self._label, size.width / 2.0, (size.height - height) / 2.0,
              INK if self._value else TEXT_SOFT, face, align='center')

  def _on_enter(self, event):
    self._hover = True
    self.Refresh()

  def _on_leave(self, event):
    self._hover = False
    self.Refresh()

  def _on_left_down(self, event):
    self._value = not self._value
    self.Refresh()
    self._on_toggle(self._value)


class Readout(Painted):
  """Caption plus a monospaced value, placed under a fader or knob."""

  def __init__(self, parent, surface, caption, value='--', width=64, height=40, size=13):
    Painted.__init__(self, parent, surface, wx.Size(parent.FromDIP(width), parent.FromDIP(height)))
    self._caption = caption
    self._value = value
    self._size = size

  def set_value(self, value):
    value = str(value)
    if value != self._value:
      self._value = value
      self.Refresh()

  def paint(self, gc, size):
    draw_tracked_text(gc, self._caption.upper(), size.width / 2.0, self.FromDIP(1), TEXT_FAINT,
                      font(8, SEMIBOLD), tracking=self.FromDIP(1), align='center')
    draw_text(gc, self._value, size.width / 2.0, self.FromDIP(15), TEXT,
              font(self._size, SEMIBOLD, mono=True), align='center')


class Caption(Painted):
  """Small uppercase section label."""

  def __init__(self, parent, surface, text, height=15):
    Painted.__init__(self, parent, surface, wx.Size(parent.FromDIP(80), parent.FromDIP(height)))
    self._text = text

  def paint(self, gc, size):
    draw_tracked_text(gc, self._text.upper(), 0, self.FromDIP(1), TEXT_FAINT,
                      font(8, SEMIBOLD), tracking=self.FromDIP(1))


class NavRail(Painted):
  """Vertical page selector."""

  ITEM_HEIGHT = 40
  WIDTH = 158

  def __init__(self, parent, items, index, on_select):
    Painted.__init__(self, parent, NAV_BG, wx.Size(parent.FromDIP(self.WIDTH), -1))
    self._items = list(items)
    self._index = index
    self._on_select = on_select
    self._hover = None
    self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
    self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
    self.Bind(wx.EVT_MOTION, self._on_motion)
    self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)

  def set_selection(self, index):
    self._index = index
    self.Refresh()

  def _item_at(self, y):
    index = int((y - self.FromDIP(44)) // self.FromDIP(self.ITEM_HEIGHT + 6))
    return index if 0 <= index < len(self._items) else None

  def paint(self, gc, size):
    draw_tracked_text(gc, 'Panel', self.FromDIP(22), self.FromDIP(22), TEXT_FAINT,
                      font(8, SEMIBOLD), tracking=self.FromDIP(1))
    face = font(11, SEMIBOLD)
    for index, label in enumerate(self._items):
      y = self.FromDIP(44) + index * self.FromDIP(self.ITEM_HEIGHT + 6)
      height = self.FromDIP(self.ITEM_HEIGHT)
      selected = index == self._index
      gc.SetPen(wx.TRANSPARENT_PEN)
      if selected or index == self._hover:
        gc.SetBrush(wx.Brush(NAV_ACTIVE if selected else NAV_HOVER))
        gc.DrawRoundedRectangle(self.FromDIP(12), y, size.width - self.FromDIP(24), height, self.FromDIP(9))
      if selected:
        bar = self.FromDIP(16)
        gc.SetBrush(wx.Brush(ACCENT))
        gc.DrawRoundedRectangle(self.FromDIP(22), y + (height - bar) / 2.0, self.FromDIP(3), bar,
                                self.FromDIP(2))
      text_y = y + (height - text_height(gc, label, face)) / 2.0
      draw_text(gc, label, self.FromDIP(36), text_y, TEXT if selected else TEXT_DIM, face)

  def _on_motion(self, event):
    index = self._item_at(event.GetY())
    if index != self._hover:
      self._hover = index
      self.Refresh()

  def _on_leave(self, event):
    self._hover = None
    self.Refresh()

  def _on_left_down(self, event):
    index = self._item_at(event.GetY())
    if index is not None and index != self._index:
      self._index = index
      self.Refresh()
      self._on_select(index)


#
# Containers
#

class Header(Painted):
  """Title bar with device identity and connection state."""

  HEIGHT = 76

  def __init__(self, parent, title, subtitle, status=None, status_colour=OK):
    Painted.__init__(self, parent, BG, wx.Size(-1, parent.FromDIP(self.HEIGHT)))
    self._title = title
    self._subtitle = subtitle
    self._status = status
    self._status_colour = status_colour

  def set_status(self, status, colour=OK):
    self._status = status
    self._status_colour = colour
    self.Refresh()

  def paint(self, gc, size):
    draw_text(gc, self._title, self.FromDIP(22), self.FromDIP(20), TEXT, font(15, SEMIBOLD))
    draw_text(gc, self._subtitle, self.FromDIP(22), self.FromDIP(44), TEXT_DIM, font(9))

    if self._status:
      face = font(9, SEMIBOLD)
      gc.SetFont(face, TEXT)
      width = gc.GetTextExtent(self._status)[0] + self.FromDIP(34)
      height = self.FromDIP(26)
      x = size.width - self.FromDIP(22) - width
      y = (size.height - height) / 2.0
      gc.SetPen(wx.Pen(self._status_colour, 1))
      gc.SetBrush(wx.Brush(self._status_colour.ChangeLightness(12)))
      gc.DrawRoundedRectangle(x, y, width, height, height / 2.0)
      gc.SetPen(wx.TRANSPARENT_PEN)
      gc.SetBrush(wx.Brush(self._status_colour))
      draw_circle(gc, x + self.FromDIP(13), y + height / 2.0, self.FromDIP(4))
      draw_text(gc, self._status, x + self.FromDIP(23), (size.height - self.FromDIP(13)) / 2.0,
                self._status_colour, face)

    gc.SetPen(wx.Pen(CARD_BORDER, 1))
    gc.StrokeLine(0, size.height - 0.5, size.width, size.height - 0.5)


class Card(Painted):
  """Rounded panel with a tracked title and an optional right aligned note."""

  PADDING = 16
  TITLE_HEIGHT = 52

  def __init__(self, parent, title, note=None, min_width=None):
    Painted.__init__(self, parent, BG)
    self._title = title
    self._note = note
    self._body = wx.BoxSizer(wx.VERTICAL)
    outer = wx.BoxSizer(wx.VERTICAL)
    outer.AddSpacer(self.FromDIP(self.TITLE_HEIGHT))
    outer.Add(self._body, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
              border=self.FromDIP(self.PADDING))
    self.SetSizer(outer)
    if min_width is not None:
      self.SetMinSize(wx.Size(self.FromDIP(min_width), -1))

  def add(self, window, proportion=0, flag=wx.EXPAND, border=0):
    self._body.Add(window, proportion, flag, border)

  def add_sizer(self, sizer, proportion=0, flag=wx.EXPAND, border=0):
    self._body.Add(sizer, proportion, flag, border)

  def set_note(self, note):
    self._note = note
    self.Refresh()

  def paint(self, gc, size):
    gc.SetPen(wx.Pen(CARD_BORDER, 1))
    gc.SetBrush(wx.Brush(CARD))
    gc.DrawRoundedRectangle(0.5, 0.5, size.width - 1, size.height - 1, self.FromDIP(14))
    draw_tracked_text(gc, self._title.upper(), self.FromDIP(self.PADDING), self.FromDIP(18), TEXT_DIM,
                      font(9, SEMIBOLD), tracking=self.FromDIP(2))
    if self._note:
      draw_tracked_text(gc, self._note.upper(), size.width - self.FromDIP(self.PADDING),
                        self.FromDIP(18), ACCENT, font(8, SEMIBOLD), tracking=self.FromDIP(1),
                        align='right')


class Page(Painted):
  """Page shell: heading, subtitle and a content area."""

  PADDING = 22
  HEADING_HEIGHT = 62

  def __init__(self, parent, title, subtitle):
    Painted.__init__(self, parent, BG)
    self._title = title
    self._subtitle = subtitle
    self._body = wx.BoxSizer(wx.HORIZONTAL)
    outer = wx.BoxSizer(wx.VERTICAL)
    outer.AddSpacer(self.FromDIP(self.HEADING_HEIGHT))
    outer.Add(self._body, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
              border=self.FromDIP(self.PADDING))
    self.SetSizer(outer)

  def add(self, window, proportion=1, flag=wx.EXPAND | wx.RIGHT, border=18):
    self._body.Add(window, proportion, flag, border)

  def add_top_aligned(self, window, border=18):
    """Let the card keep its natural height instead of stretching the body."""
    self._body.Add(window, 1, wx.ALIGN_TOP | wx.RIGHT, border)

  def paint(self, gc, size):
    x = self.FromDIP(self.PADDING)
    draw_text(gc, self._title, x, self.FromDIP(18), TEXT, font(17, SEMIBOLD))
    draw_text(gc, self._subtitle, x, self.FromDIP(42), TEXT_DIM, font(9.5))


class TextLine(Painted):
  """Bullet line of body copy."""

  def __init__(self, parent, surface, text, height=22, colour=TEXT_DIM):
    Painted.__init__(self, parent, surface, wx.Size(parent.FromDIP(220), parent.FromDIP(height)))
    self._text = text
    self._colour = colour

  def paint(self, gc, size):
    draw_text(gc, '\u2022', 0, self.FromDIP(2), ACCENT, font(10))
    draw_text(gc, self._text, self.FromDIP(16), self.FromDIP(2), self._colour, font(10))


#
# Panels
#

class InputCard(Card):
  """Gain, input type and per-input processing for one channel."""

  def __init__(self, parent, input_, writer, on_structure_change):
    Card.__init__(self, parent, 'Input {}'.format(input_.number), min_width=340)
    self._input = input_
    self._writer = writer
    self._on_structure_change = on_structure_change

    left = wx.BoxSizer(wx.VERTICAL)
    self._fader = Fader(self, CARD, input_.min_level or 0, input_.max_level or 1, input_.level or 0,
                        self._on_gain, height=182)
    self._readout = Readout(self, CARD, 'Gain', width=76)
    left.Add(self._fader, 1, wx.ALIGN_CENTER_HORIZONTAL)
    left.Add(self._readout, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 10)

    right = wx.BoxSizer(wx.VERTICAL)
    right.Add(Caption(self, CARD, 'Input type'))
    self._type = Segmented(self, CARD, InputType.str_list(), input_.type_.value, self._on_type, columns=2)
    right.Add(self._type, 0, wx.EXPAND | wx.TOP, 7)
    right.Add(Caption(self, CARD, 'Processing'), 0, wx.TOP, 18)

    self._switches = {}
    rows = (
      ('phantom_power', 'Phantom Power', ACCENT),
      ('phase', 'Phase', ACCENT),
      ('softlimit', 'Soft Limit', WARN),
      ('group', 'Group', ACCENT),
    )
    for key, label, colour in rows:
      row = SwitchRow(self, CARD, label, False, self._toggle_slot(key), active_colour=colour)
      self._switches[key] = row
      right.Add(row, 0, wx.EXPAND | wx.TOP, 2)

    body = wx.BoxSizer(wx.HORIZONTAL)
    body.Add(left, 0, wx.EXPAND | wx.RIGHT, 22)
    body.Add(right, 1, wx.EXPAND)
    self.add_sizer(body, 1, wx.EXPAND)
    self.refresh()

  def _toggle_slot(self, slot):
    def toggle(value):
      getattr(self._input, 'toggle_' + slot)()
      self._on_structure_change()
    return toggle

  def _on_gain(self, value, final):
    self._readout.set_value(value)
    self._writer.submit((self._input, 'level'), lambda: setattr(self._input, 'level', value),
                        immediate=final)

  def _on_type(self, index):
    self._input.type_ = index
    self._on_structure_change()

  def refresh(self):
    input_ = self._input
    self.set_note(str(input_.type_))
    self._type.set_selection(input_.type_.value)
    if input_.has_level_control:
      self._fader.set_range(input_.min_level, input_.max_level)
      self._fader.set_value(input_.level)
      self._fader.set_interactive(True)
      self._readout.set_value(input_.level)
    else:
      self._fader.set_interactive(False)
      self._readout.set_value('--')
    self._switches['phantom_power'].set_value(input_.phantom_power_state)
    self._switches['phantom_power'].set_interactive(input_.type_ == InputType.MICROPHONE)
    self._switches['phase'].set_value(input_.phase_state)
    self._switches['softlimit'].set_value(input_.softlimit_state)
    self._switches['group'].set_value(input_.group_state)


class InputsPage(Page):
  def __init__(self, parent, device, writer):
    Page.__init__(self, parent, 'Inputs', 'Preamps, input type and per-channel processing')
    self._cards = [
      InputCard(self, input_, writer, self.refresh) for input_ in device.inputs
    ]
    for index, card in enumerate(self._cards):
      self.add_top_aligned(card, border=0 if index == len(self._cards) - 1 else 18)

  def refresh(self):
    for card in self._cards:
      card.refresh()


class OutputCard(Card):
  """Level, source and monitoring state for one output pair."""

  def __init__(self, parent, output, writer):
    Card.__init__(self, parent, str(output.type_), min_width=340)
    self._output = output
    self._writer = writer

    left = wx.BoxSizer(wx.VERTICAL)
    self._fader = Fader(self, CARD, output.min_level, output.max_level, output.level, self._on_level,
                        height=182)
    self._readout = Readout(self, CARD, 'Level', width=76)
    left.Add(self._fader, 1, wx.ALIGN_CENTER_HORIZONTAL)
    left.Add(self._readout, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 10)
    self._readout.set_value(output.level)

    right = wx.BoxSizer(wx.VERTICAL)
    right.Add(Caption(self, CARD, 'Source'))
    self._source = Segmented(self, CARD, OutputSource.str_list(), output.source.value,
                             self._on_source, columns=3)
    right.Add(self._source, 0, wx.EXPAND | wx.TOP, 7)

    if output.type_ == OutputType.SPEAKERS:
      right.Add(Caption(self, CARD, 'Speaker output'), 0, wx.TOP, 18)
      self._speaker = Segmented(self, CARD, SpeakerOutputType.str_list(),
                                output.speaker_output_type.value, self._on_speaker_type, columns=2)
      right.Add(self._speaker, 0, wx.EXPAND | wx.TOP, 7)
    else:
      self._speaker = None

    right.Add(Caption(self, CARD, 'Monitoring'), 0, wx.TOP, 18)
    self._mute = SwitchRow(self, CARD, 'Mute', output.mute_state, self._toggle('mute'), active_colour=DANGER)
    self._dim = SwitchRow(self, CARD, 'Dim', output.dim_state, self._toggle('dim'), active_colour=WARN)
    self._mono = SwitchRow(self, CARD, 'Mono', output.mono_state, self._toggle('mono'))
    for row in (self._mute, self._dim, self._mono):
      right.Add(row, 0, wx.EXPAND | wx.TOP, 2)

    body = wx.BoxSizer(wx.HORIZONTAL)
    body.Add(left, 0, wx.EXPAND | wx.RIGHT, 22)
    body.Add(right, 1, wx.EXPAND)
    self.add_sizer(body, 1, wx.EXPAND)

  def _toggle(self, name):
    def toggle(value):
      getattr(self._output, 'toggle_' + name)()
    return toggle

  def _on_level(self, value, final):
    self._readout.set_value(value)
    self._writer.submit((self._output, 'level'), lambda: setattr(self._output, 'level', value),
                        immediate=final)

  def _on_source(self, index):
    self._output.source = index

  def _on_speaker_type(self, index):
    self._output.speaker_output_type = index


class OutputsPage(Page):
  def __init__(self, parent, device, writer):
    Page.__init__(self, parent, 'Outputs', 'Output level, source routing and monitoring')
    outputs = [OutputCard(self, output, writer) for output in device.outputs]
    for index, card in enumerate(outputs):
      self.add_top_aligned(card, border=0 if index == len(outputs) - 1 else 18)


def mixer_strip_title(channel):
  if channel.type_ == ChannelType.INPUT:
    return 'Input {}'.format(channel.index + 1)
  return {
    ChannelType.SOFTWARE_RETURN: 'Software',
    ChannelType.MASTER: 'Master',
  }[channel.type_]


class Block(Painted):
  """Invisible container used to put a common height on stacked strips."""

  def __init__(self, parent, surface):
    Painted.__init__(self, parent, surface)
    self._inner = wx.BoxSizer(wx.VERTICAL)
    self.SetSizer(self._inner)

  def add(self, window, proportion=0, flag=wx.EXPAND, border=0):
    self._inner.Add(window, proportion, flag, border)

  def add_sizer(self, sizer, proportion=0, flag=wx.EXPAND, border=0):
    self._inner.Add(sizer, proportion, flag, border)

  def add_stretch(self):
    self._inner.AddStretchSpacer()

  def paint(self, gc, size):
    pass


class MixerStrip(Card):
  """One mixer channel: level, and pan or source depending on the channel type."""

  def __init__(self, parent, channel, writer):
    Card.__init__(self, parent, mixer_strip_title(channel), min_width=140)
    self._channel = channel
    self._writer = writer

    # Everything above the fader lives in a block whose height is equalised
    # across the strips, so the fader tracks line up like a real mixer.
    self._top = Block(self, CARD)
    self.add(self._top, 0, wx.EXPAND)

    if channel.type_ == ChannelType.INPUT:
      self._knob = Knob(self._top, CARD, channel.min_pan, channel.max_pan, channel.pan,
                        self._on_pan, center=0, diameter=76)
      self._top.add(self._knob, 0, wx.ALIGN_CENTER_HORIZONTAL)
    else:
      self._knob = None

    self._source = None
    if channel.type_ == ChannelType.SOFTWARE_RETURN:
      self._top.add(Caption(self._top, CARD, 'Source'), 0, wx.EXPAND)
      self._source = Segmented(self._top, CARD, SoftwareReturnSource.str_list(),
                               channel.source.value, self._on_source, columns=2, cell_height=28)
      self._top.add(self._source, 0, wx.EXPAND | wx.TOP, 6)

    self._mute = None
    self._solo = None
    if channel.type_ != ChannelType.MASTER:
      self._top.add_stretch()
      chips = wx.BoxSizer(wx.HORIZONTAL)
      self._mute = ChipButton(self._top, CARD, 'Mute', DANGER, self._toggle('mute'))
      self._solo = ChipButton(self._top, CARD, 'Solo', WARN, self._toggle('solo'))
      self._mute.set_value(channel.mute_state)
      self._solo.set_value(channel.solo_state)
      chips.Add(self._mute, 1, wx.EXPAND | wx.RIGHT, 6)
      chips.Add(self._solo, 1, wx.EXPAND)
      self._top.add_sizer(chips, 0, wx.EXPAND)
    else:
      self._top.add_stretch()

    self._fader = Fader(self, CARD, channel.min_level, channel.max_level, channel.level,
                        self._on_level, height=168)
    self._readout = Readout(self, CARD, 'Level', width=76)
    self._readout.set_value(channel.level)
    self.add(self._fader, 1, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 12)
    self.add(self._readout, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 8)

  def top_height(self):
    return self._top.GetBestSize().height

  def set_top_height(self, height):
    self._top.SetMinSize(wx.Size(-1, height))

  def _toggle(self, name):
    def toggle(value):
      getattr(self._channel, 'toggle_' + name)()
    return toggle

  def _on_level(self, value, final):
    self._readout.set_value(value)
    self._writer.submit((self._channel, 'level'), lambda: setattr(self._channel, 'level', value),
                        immediate=final)

  def _on_pan(self, value):
    self._writer.submit((self._channel, 'pan'), lambda: setattr(self._channel, 'pan', value))

  def _on_source(self, index):
    self._channel.source = index


class MixerPage(Page):
  def __init__(self, parent, device, writer):
    Page.__init__(self, parent, 'Mixer', 'Mix levels for the inputs, the software return and the master bus')
    strips = [MixerStrip(self, channel, writer) for channel in device.mixer_channels]
    for strip in strips:
      self.add(strip, 1, wx.EXPAND | wx.RIGHT, 14)
    height = max(strip.top_height() for strip in strips)
    for strip in strips:
      strip.set_top_height(height)


class OfflinePage(Page):
  """Shown when no Duet is attached, instead of an unhandled exception."""

  def __init__(self, parent, message):
    Page.__init__(self, parent, 'Device unavailable', message)
    card = Card(self, 'No Apogee Duet found')
    card.add(Caption(self, CARD, 'Checklist'), 0, wx.EXPAND)
    for line in (
      'Connect the Duet over USB and power it on',
      'Confirm the device is listed by lsusb as 0c60:0016',
      'Grant USB access with a udev rule if opening it fails',
    ):
      card.add(TextLine(self, CARD, line), 0, wx.EXPAND | wx.TOP, 8)
    self.add_top_aligned(card)


class MainFrame(wx.Frame):
  NAV_ITEMS = ('Inputs', 'Outputs', 'Mixer')

  def __init__(self):
    wx.Frame.__init__(self, None, title='Apogee Duet Control')
    init_theme()
    self.SetBackgroundColour(BG)
    self._writer = ThrottledWriter(self)

    root = wx.Panel(self)
    root.SetBackgroundColour(BG)
    vertical = wx.BoxSizer(wx.VERTICAL)

    try:
      device = ApogeeDuet()
    except ValueError as error:
      self._device = None
      self._header = Header(root, 'Apogee Duet', 'USB control panel', 'Disconnected', DANGER)
      vertical.Add(self._header, 0, wx.EXPAND)
      vertical.Add(OfflinePage(root, str(error)), 1, wx.EXPAND)
      root.SetSizer(vertical)
      self._finish()
      return

    self._device = device
    self._header = Header(root, 'Apogee Duet', self._device_label(device), 'Connected', OK)
    vertical.Add(self._header, 0, wx.EXPAND)

    rows = wx.BoxSizer(wx.HORIZONTAL)
    self._nav = NavRail(root, self.NAV_ITEMS, 0, self._select_page)
    rows.Add(self._nav, 0, wx.EXPAND)

    self._book = wx.Simplebook(root)
    self._book.SetBackgroundColour(BG)
    self._book.AddPage(InputsPage(self._book, device, self._writer), 'Inputs')
    self._book.AddPage(OutputsPage(self._book, device, self._writer), 'Outputs')
    self._book.AddPage(MixerPage(self._book, device, self._writer), 'Mixer')
    rows.Add(self._book, 1, wx.EXPAND)

    vertical.Add(rows, 1, wx.EXPAND)
    root.SetSizer(vertical)
    self._finish()

  @staticmethod
  def _device_label(device):
    parts = [device.product_name or 'Duet USB']
    if device.serial_number:
      parts.append(device.serial_number)
    return '  \u00b7  '.join(parts)

  def _finish(self):
    self.SetSize(self.FromDIP(wx.Size(1040, 600)))
    self.SetMinSize(self.FromDIP(wx.Size(900, 560)))
    self.Bind(wx.EVT_CLOSE, self._on_close)

  def _select_page(self, index):
    self._book.SetSelection(index)

  def _on_close(self, event):
    self._writer.flush()
    event.Skip()


def run():
  app = wx.App(False)
  init_theme()
  frame = MainFrame()
  frame.Show()
  app.MainLoop()


if __name__ == '__main__':
  run()
