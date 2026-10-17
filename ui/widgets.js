// Panel controls. Every factory returns an object holding the root `element`
// and a `set` method; `set` only repaints, user input alone reports changes.

const SVG = 'http://www.w3.org/2000/svg';

export function h(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === 'class') element.className = value;
    else if (key.startsWith('on')) element.addEventListener(key.slice(2).toLowerCase(), value);
    else element.setAttribute(key, value === true ? '' : value);
  }
  element.append(...children.flat().filter((child) => child != null && child !== false));
  return element;
}

function svg(tag, attributes = {}) {
  const element = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
  return element;
}

const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

/** Signed dB label with a typographic minus. */
export function formatDb(value, unit = '') {
  const number = value > 0 ? `+${value}` : value < 0 ? `−${-value}` : '0';
  return unit ? `${number} ${unit}` : number;
}

/** Multiples of `step` inside the range, plus the ends when they stand apart. */
// Track positions (0 at the bottom, 1 at the top) for a value in [low, high].
const linearTaper = {
  ratio: (value, low, high) => (high > low ? (value - low) / (high - low) : 0),
  value: (ratio, low, high) => low + ratio * (high - low),
};

// PipeWire's volume curve (the amplitude is the volume cubed), so a dB fader
// sits where the system volume slider does: the top is 100 %, -6 dB is 79 %.
export const volumeTaper = {
  ratio: (value, low, high) => 10 ** ((value - high) / 60),
  value: (ratio, low, high) => Math.max(low, high + 60 * Math.log10(Math.max(ratio, 1e-6))),
};

function scaleMarks(low, high, step) {
  const marks = [];
  for (let mark = Math.ceil(low / step) * step; mark <= high; mark += step) marks.push(mark);
  if (!marks.length || marks[0] - low >= step / 2) marks.unshift(low);
  if (high - marks.at(-1) >= step / 2) marks.push(high);
  return marks;
}

// Wheel deltas arrive as one large step per mouse notch or as a stream of
// small ones from a touchpad; both turn into one value step per notch.
function wheelSteps(state, event) {
  if (Math.abs(event.deltaY) >= 50) {
    state.rest = 0;
    return -Math.sign(event.deltaY);
  }
  state.rest += event.deltaY;
  const steps = Math.trunc(state.rest / 50);
  state.rest -= steps * 50;
  return -steps;
}

export function caption(text) {
  return h('p', { class: 'caption' }, text);
}

/** Vertical fader; `onChange(value, final)` fires for every change. */
export function fader({ min, max, value, step, marks, taper = linearTaper, label, signed = true, onChange }) {
  let low = min;
  let high = max;
  let markStep = step;
  let current = clamp(value, low, high);
  let enabled = true;
  let dragging = false;
  const wheel = { rest: 0 };

  const fill = h('div', { class: 'fader-fill' });
  const thumb = h('div', { class: 'fader-thumb' });
  const rail = h('div', { class: 'fader-rail' }, h('div', { class: 'fader-track' }, fill), thumb);
  const scale = h('div', { class: 'fader-scale', 'aria-hidden': 'true' });
  const element = h('div', {
    class: 'fader',
    role: 'slider',
    tabindex: 0,
    'aria-label': label,
    'aria-orientation': 'vertical',
  }, scale, rail);

  const ratio = (number) => taper.ratio(number, low, high);
  const format = (number, unit) => {
    if (signed) return formatDb(number, unit);
    return unit ? `${number} ${unit}` : String(number);
  };

  function paint() {
    element.style.setProperty('--ratio', ratio(current));
    element.setAttribute('aria-valuemin', low);
    element.setAttribute('aria-valuemax', high);
    element.setAttribute('aria-valuenow', current);
    element.setAttribute('aria-valuetext', format(current, 'dB'));
  }

  function paintScale() {
    scale.replaceChildren(...(marks ?? scaleMarks(low, high, markStep)).map((mark) => {
      const tick = h('span', { class: 'fader-mark' }, format(mark));
      tick.style.setProperty('--ratio', ratio(mark));
      return tick;
    }));
  }

  function change(next, final) {
    next = clamp(Math.round(next), low, high);
    if (next === current) return;
    current = next;
    paint();
    onChange(current, final);
  }

  function valueAt(clientY) {
    const box = rail.getBoundingClientRect();
    const inset = thumb.offsetHeight / 2 + 6;
    const span = box.height - 2 * inset;
    if (span <= 0) return low;
    return taper.value(clamp((box.bottom - inset - clientY) / span, 0, 1), low, high);
  }

  element.addEventListener('pointerdown', (event) => {
    if (!enabled || event.button !== 0) return;
    element.focus();
    dragging = true;
    element.classList.add('dragging');
    element.setPointerCapture(event.pointerId);
    change(valueAt(event.clientY), false);
  });
  element.addEventListener('pointermove', (event) => {
    if (dragging) change(valueAt(event.clientY), false);
  });
  const release = () => {
    if (!dragging) return;
    dragging = false;
    element.classList.remove('dragging');
    // The last drag value may still be throttled: always land it now.
    onChange(current, true);
  };
  element.addEventListener('pointerup', release);
  element.addEventListener('lostpointercapture', release);
  element.addEventListener('wheel', (event) => {
    event.preventDefault();
    if (!enabled) return;
    const steps = wheelSteps(wheel, event);
    if (steps) change(current + steps * (event.shiftKey || event.ctrlKey ? 5 : 1), true);
  }, { passive: false });
  element.addEventListener('keydown', (event) => {
    if (!enabled) return;
    const page = Math.max(1, Math.floor((high - low) / 10));
    const moves = {
      ArrowUp: current + 1,
      ArrowRight: current + 1,
      ArrowDown: current - 1,
      ArrowLeft: current - 1,
      PageUp: current + page,
      PageDown: current - page,
      Home: low,
      End: high,
    };
    if (!(event.key in moves)) return;
    event.preventDefault();
    change(moves[event.key], true);
  });

  paintScale();
  paint();

  return {
    element,
    set(next) {
      if (dragging) return;
      current = clamp(next, low, high);
      paint();
    },
    setRange(min, max, step = markStep) {
      low = min;
      high = max;
      markStep = step;
      current = clamp(current, low, high);
      paintScale();
      paint();
    },
    setEnabled(next) {
      enabled = next;
      element.classList.toggle('disabled', !enabled);
      element.setAttribute('aria-disabled', String(!enabled));
      element.tabIndex = enabled ? 0 : -1;
    },
  };
}

/** Pan knob: drag vertically, double click recentres. */
export function knob({ min, max, value, center = 0, label, onChange }) {
  const DOTS = 21;
  const SWEEP = 135;
  let current = clamp(value, min, max);
  let origin = null;
  const wheel = { rest: 0 };

  const ratio = (number) => (number - min) / (max - min);
  const angle = (number) => ((90 + SWEEP - 2 * SWEEP * ratio(number)) * Math.PI) / 180;
  const point = (radius, number) => [50 + radius * Math.cos(angle(number)), 50 - radius * Math.sin(angle(number))];

  const dots = Array.from({ length: DOTS }, (_, index) => {
    const number = min + ((max - min) * index) / (DOTS - 1);
    const [cx, cy] = point(44, number);
    return { number, element: svg('circle', { class: 'knob-dot', cx, cy, r: 2.6 }) };
  });
  const pointer = svg('line', { class: 'knob-pointer' });
  const graphic = svg('svg', { viewBox: '0 0 100 100', 'aria-hidden': 'true' });
  graphic.append(...dots.map((dot) => dot.element), svg('circle', { class: 'knob-body', cx: 50, cy: 50, r: 33 }), pointer);
  const element = h('div', {
    class: 'knob',
    role: 'slider',
    tabindex: 0,
    'aria-label': label,
    'aria-valuemin': min,
    'aria-valuemax': max,
  }, graphic);

  function paint() {
    const [x1, y1] = point(12, current);
    const [x2, y2] = point(27, current);
    Object.entries({ x1, y1, x2, y2 }).forEach(([key, number]) => pointer.setAttribute(key, number.toFixed(2)));
    const [from, to] = current < center ? [current, center] : [center, current];
    // Light the dots between the centre and the value, the way a pan reads.
    const half = (max - min) / (DOTS - 1) / 2;
    for (const dot of dots) dot.element.classList.toggle('active', dot.number >= from - half && dot.number <= to + half);
    element.setAttribute('aria-valuenow', current);
    element.setAttribute('aria-valuetext', panLabel(current));
  }

  function change(next, final) {
    next = clamp(Math.round(next), min, max);
    if (next === current) return;
    current = next;
    paint();
    onChange(current, final);
  }

  element.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    element.focus();
    origin = { y: event.clientY, value: current };
    element.setPointerCapture(event.pointerId);
  });
  element.addEventListener('pointermove', (event) => {
    if (origin) change(origin.value + ((origin.y - event.clientY) * (max - min)) / 150, false);
  });
  const release = () => {
    if (!origin) return;
    origin = null;
    onChange(current, true);
  };
  element.addEventListener('pointerup', release);
  element.addEventListener('lostpointercapture', release);
  element.addEventListener('dblclick', () => change(center, true));
  element.addEventListener('wheel', (event) => {
    event.preventDefault();
    const steps = wheelSteps(wheel, event);
    if (steps) change(current + steps * (event.shiftKey || event.ctrlKey ? 4 : 1), true);
  }, { passive: false });
  element.addEventListener('keydown', (event) => {
    const moves = {
      ArrowUp: current + 1,
      ArrowRight: current + 1,
      ArrowDown: current - 1,
      ArrowLeft: current - 1,
      Home: min,
      End: max,
      Delete: center,
    };
    if (!(event.key in moves)) return;
    event.preventDefault();
    change(moves[event.key], true);
  });

  paint();

  return {
    element,
    set(next) {
      if (origin) return;
      current = clamp(next, min, max);
      paint();
    },
  };
}

export function panLabel(value) {
  if (value === 0) return 'C';
  return value < 0 ? `L${-value}` : `R${value}`;
}

/** Segmented selector laid out as a grid of `columns`. */
export function segmented({ labels, index = 0, columns = labels.length, label, onSelect }) {
  let current = index;
  const buttons = labels.map((text, position) => h('button', {
    type: 'button',
    class: 'segment',
    role: 'radio',
    onClick: () => choose(position),
  }, text));
  const element = h('div', { class: 'segmented', role: 'radiogroup', 'aria-label': label }, buttons);
  element.style.setProperty('--columns', columns);

  function paint() {
    buttons.forEach((button, position) => {
      button.setAttribute('aria-checked', String(position === current));
      button.tabIndex = position === current ? 0 : -1;
    });
  }

  function choose(position) {
    if (position === current) return;
    current = position;
    paint();
    onSelect(position);
  }

  element.addEventListener('keydown', (event) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (!step) return;
    event.preventDefault();
    const next = clamp(current + step, 0, labels.length - 1);
    choose(next);
    buttons[next].focus();
  });

  paint();

  return {
    element,
    set(position) {
      current = position;
      paint();
    },
  };
}

/** Label plus a pill switch; the whole row is the hit target. */
export function switchRow({ label, hint, tone = 'accent', onToggle }) {
  let value = false;
  const element = h('button', {
    type: 'button',
    class: 'switch-row',
    role: 'switch',
    'aria-label': label,
    'data-tone': tone,
    onClick: () => {
      value = !value;
      paint();
      onToggle(value);
    },
  },
  h('span', { class: 'switch-label' }, label, hint && h('span', { class: 'switch-hint' }, hint)),
  h('span', { class: 'switch', 'aria-hidden': 'true' }, h('span', { class: 'switch-knob' })));

  function paint() {
    element.setAttribute('aria-checked', String(value));
  }

  paint();

  return {
    element,
    set(next) {
      value = Boolean(next);
      paint();
    },
    setEnabled(enabled) {
      element.disabled = !enabled;
    },
  };
}

/** Compact toggle used for the mixer mute and solo buttons. */
export function chip({ label, tone, onToggle }) {
  let value = false;
  const element = h('button', {
    type: 'button',
    class: 'chip',
    'data-tone': tone,
    onClick: () => {
      value = !value;
      paint();
      onToggle(value);
    },
  }, label);

  function paint() {
    element.setAttribute('aria-pressed', String(value));
  }

  paint();

  return {
    element,
    set(next) {
      value = Boolean(next);
      paint();
    },
  };
}

/** Caption plus a monospaced value, placed under a fader or knob. */
export function readout(label, value = '—') {
  const output = h('output', { class: 'readout-value' }, value);
  return {
    element: h('div', { class: 'readout' }, h('span', { class: 'caption' }, label), output),
    set(text) {
      output.textContent = text;
    },
  };
}

/** Rounded panel with a tracked title and an optional right aligned note. */
export function card({ title, className = '' }) {
  const note = h('span', { class: 'card-note' });
  const header = h('header', { class: 'card-header' }, h('h3', {}, title), note);
  const element = h('section', { class: `card ${className}`.trim() }, header);
  return {
    element,
    setNote(text, tone = 'accent') {
      note.textContent = text;
      note.dataset.tone = tone;
    },
  };
}
