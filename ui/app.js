// Apogee Duet control panel. The Rust side owns the USB protocol and the
// device state; this module renders that state and sends changes back.

import { caption, card, chip, fader, formatDb, h, knob, panLabel, readout, segmented, switchRow, volumeTaper } from './widgets.js';

const { invoke } = window.__TAURI__.core;
const { getCurrentWindow } = window.__TAURI__.window;

const INPUT_TYPES = [['line_4dbu', '+4dBu'], ['line_10dbv', '-10dBV'], ['microphone', 'Microphone'], ['instrument', 'Instrument']];
const OUTPUT_SOURCES = [['playback_1_2', '1-2'], ['playback_3_4', '3-4'], ['mixer', 'Mixer']];
const SPEAKER_TYPES = [['line_4dbu', '+4dBu'], ['line_10dbv', '-10dBV']];
const RETURN_SOURCES = [['playback_1_2', '1-2'], ['playback_3_4', '3-4']];
const GAIN_STEP = { microphone: 25, instrument: 20 };

// A USB control transfer costs ~3 ms while a drag emits far more events than
// that, so drags write at most once per interval and the release lands at once.
const WRITE_INTERVAL = 60;
const RETRY_INTERVAL = 2500;
const POLL_INTERVAL = 1000;
const OFFLINE_CODES = new Set(['not_found', 'access', 'disconnected']);

const labelOf = (options, value) => options.find(([key]) => key === value)?.[1] ?? value;
const indexOf = (options, value) => options.findIndex(([key]) => key === value);
const toError = (error) => (typeof error === 'string' ? { code: 'invalid', message: error } : error);

/**
 * Serialises device exchanges. A newer change for the same target replaces
 * the pending one, so a drag never queues more than one write per control,
 * and polls only run while nothing is waiting to be written.
 */
class Writer {
  #pending = new Map();
  #timer = 0;
  #running = null;
  #generation = 0;

  constructor({ onIdle, onError }) {
    this.onIdle = onIdle;
    this.onError = onError;
  }

  submit(key, change, immediate) {
    this.#generation += 1;
    this.#pending.delete(key);
    this.#pending.set(key, change);
    if (immediate) this.flush();
    else if (!this.#timer) this.#timer = setTimeout(() => this.flush(), WRITE_INTERVAL);
  }

  flush() {
    clearTimeout(this.#timer);
    this.#timer = 0;
    if (this.#running) return this.#running.then(() => this.flush());
    return this.#run(() => this.#drain());
  }

  /** Reads back the registers the front knob and the system mixer change. */
  poll() {
    if (this.#running || this.#pending.size || this.#timer) return;
    const generation = this.#generation;
    this.#run(async () => {
      try {
        const state = await invoke('poll');
        // A change sent meanwhile is newer than what the poll read.
        if (generation === this.#generation) this.onIdle(state);
      } catch (error) {
        this.reset();
        this.onError(toError(error));
      }
    });
  }

  reset() {
    clearTimeout(this.#timer);
    this.#timer = 0;
    this.#pending.clear();
  }

  #run(exchange) {
    this.#running = exchange().finally(() => {
      this.#running = null;
    });
    return this.#running;
  }

  async #drain() {
    let state = null;
    while (this.#pending.size) {
      const [key, change] = this.#pending.entries().next().value;
      this.#pending.delete(key);
      try {
        state = await invoke('apply', { change });
      } catch (error) {
        this.reset();
        this.onError(toError(error));
        return;
      }
    }
    // Repaint from the device only once nothing newer is waiting, so an
    // older answer never pulls a control back mid-gesture.
    if (state && !this.#timer) this.onIdle(state);
  }
}

function page(title, subtitle, ...body) {
  return h('section', { class: 'page' },
    h('header', { class: 'page-heading' }, h('h2', {}, title), h('p', {}, subtitle)),
    ...body);
}

function inputCard(index, writer) {
  const shell = card({ title: `Input ${index + 1}` });
  const gainReadout = readout('Gain');
  const gain = fader({
    min: 0,
    max: 75,
    value: 0,
    step: GAIN_STEP.microphone,
    label: `Input ${index + 1} gain`,
    signed: false,
    onChange(value, final) {
      gainReadout.set(`${value} dB`);
      writer.submit(`gain:${index}`, { kind: 'inputGain', input: index, value }, final);
    },
  });
  const type = segmented({
    labels: INPUT_TYPES.map(([, label]) => label),
    columns: 2,
    label: 'Input type',
    onSelect: (position) => writer.submit(`type:${index}`, { kind: 'inputType', input: index, value: INPUT_TYPES[position][0] }, true),
  });
  const toggle = (kind, label, options = {}) => switchRow({
    label,
    ...options,
    onToggle: (value) => writer.submit(`${kind}:${index}`, kind === 'group' ? { kind, value } : { kind, input: index, value }, true),
  });
  const phantomPower = toggle('phantomPower', 'Phantom Power', { hint: '48V' });
  const phase = toggle('phase', 'Phase');
  const softLimit = toggle('softLimit', 'Soft Limit', { tone: 'warn' });
  const group = toggle('group', 'Group');

  shell.element.append(h('div', { class: 'channel-body' },
    h('div', { class: 'level-column' }, gain.element, gainReadout.element),
    h('div', { class: 'control-column' },
      caption('Input type'), type.element,
      caption('Processing'), phantomPower.element, phase.element, softLimit.element, group.element)));

  return {
    element: shell.element,
    update({ inputs }) {
      const input = inputs[index];
      shell.setNote(labelOf(INPUT_TYPES, input.inputType));
      type.set(indexOf(INPUT_TYPES, input.inputType));
      if (input.gain == null) {
        gain.setEnabled(false);
        gain.set(0);
        gainReadout.set('—');
      } else {
        gain.setRange(0, input.gainMax, GAIN_STEP[input.inputType]);
        gain.setEnabled(true);
        gain.set(input.gain);
        gainReadout.set(`${input.gain} dB`);
      }
      phantomPower.set(input.phantomPower);
      phantomPower.setEnabled(input.inputType === 'microphone');
      phase.set(input.phase);
      softLimit.set(input.softLimit);
      group.set(input.group);
    },
  };
}

function outputCard(index, writer, limits) {
  const speakers = index === 0;
  const shell = card({ title: speakers ? 'Speakers' : 'Headphones' });
  const levelReadout = readout('Level');
  const level = fader({
    min: limits.outputLevel[0],
    max: limits.outputLevel[1],
    value: limits.outputLevel[0],
    marks: [limits.outputLevel[0], -24, -12, -6, limits.outputLevel[1]],
    taper: volumeTaper,
    label: `${speakers ? 'Speaker' : 'Headphone'} level`,
    onChange(value, final) {
      levelReadout.set(formatDb(value, 'dB'));
      writer.submit(`level:${index}`, { kind: 'outputLevel', output: index, value }, final);
    },
  });
  const source = segmented({
    labels: OUTPUT_SOURCES.map(([, label]) => label),
    label: 'Source',
    onSelect: (position) => writer.submit(`source:${index}`, { kind: 'outputSource', output: index, value: OUTPUT_SOURCES[position][0] }, true),
  });
  const speakerType = speakers && segmented({
    labels: SPEAKER_TYPES.map(([, label]) => label),
    label: 'Speaker output',
    onSelect: (position) => writer.submit('speakerType', { kind: 'speakerOutputType', value: SPEAKER_TYPES[position][0] }, true),
  });
  const toggle = (kind, label, tone) => switchRow({
    label,
    tone,
    onToggle: (value) => writer.submit(`${kind}:${index}`, { kind, output: index, value }, true),
  });
  const mute = toggle('outputMute', 'Mute', 'danger');
  const dim = toggle('outputDim', 'Dim', 'warn');
  const mono = toggle('outputMono', 'Mono');

  shell.element.append(h('div', { class: 'channel-body' },
    h('div', { class: 'level-column' }, level.element, levelReadout.element),
    h('div', { class: 'control-column' },
      caption('Source'), source.element,
      speakerType && [caption('Speaker output'), speakerType.element],
      caption('Monitoring'), mute.element, dim.element, mono.element)));

  return {
    element: shell.element,
    update({ outputs }) {
      const output = outputs[index];
      level.set(output.level);
      levelReadout.set(formatDb(output.level, 'dB'));
      source.set(indexOf(OUTPUT_SOURCES, output.source));
      if (speakerType) speakerType.set(indexOf(SPEAKER_TYPES, output.speakerOutputType));
      mute.set(output.mute);
      dim.set(output.dim);
      mono.set(output.mono);
      if (output.mute) shell.setNote('Muted', 'danger');
      else if (output.dim) shell.setNote('Dimmed', 'warn');
      else shell.setNote('');
    },
  };
}

function mixerStrip(index, channel, writer, limits) {
  const title = { input: `Input ${index + 1}`, software_return: 'Software', master: 'Master' }[channel.kind];
  const shell = card({ title, className: 'strip' });
  const top = h('div', { class: 'strip-top' });

  let pan = null;
  let panReadout = null;
  if (channel.kind === 'input') {
    panReadout = h('output', { class: 'pan-value' }, 'C');
    pan = knob({
      min: limits.pan[0],
      max: limits.pan[1],
      value: 0,
      label: `${title} pan`,
      onChange(value, final) {
        panReadout.textContent = panLabel(value);
        writer.submit(`pan:${index}`, { kind: 'channelPan', channel: index, value }, final);
      },
    });
    top.append(h('div', { class: 'pan' }, pan.element, panReadout));
  }

  const source = channel.kind === 'software_return' && segmented({
    labels: RETURN_SOURCES.map(([, label]) => label),
    label: 'Software return source',
    onSelect: (position) => writer.submit('returnSource', { kind: 'softwareReturnSource', value: RETURN_SOURCES[position][0] }, true),
  });
  if (source) top.append(h('div', { class: 'strip-source' }, caption('Source'), source.element));

  let mute = null;
  let solo = null;
  if (channel.kind !== 'master') {
    const toggle = (kind, label, tone) => chip({
      label,
      tone,
      onToggle: (value) => writer.submit(`${kind}:${index}`, { kind, channel: index, value }, true),
    });
    mute = toggle('channelMute', 'Mute', 'danger');
    solo = toggle('channelSolo', 'Solo', 'warn');
    top.append(h('div', { class: 'chips' }, mute.element, solo.element));
  }

  const levelReadout = readout('Level');
  const level = fader({
    min: limits.channelLevel[0],
    max: limits.channelLevel[1],
    value: 0,
    step: 12,
    label: `${title} level`,
    onChange(value, final) {
      levelReadout.set(formatDb(value, 'dB'));
      writer.submit(`mix:${index}`, { kind: 'channelLevel', channel: index, value }, final);
    },
  });

  shell.element.append(top, level.element, levelReadout.element);

  return {
    element: shell.element,
    update({ mixer }) {
      const state = mixer[index];
      level.set(state.level);
      levelReadout.set(formatDb(state.level, 'dB'));
      if (pan) {
        pan.set(state.pan);
        panReadout.textContent = panLabel(state.pan);
      }
      if (source) source.set(indexOf(RETURN_SOURCES, state.source));
      mute?.set(state.mute);
      solo?.set(state.solo);
    },
  };
}

function buildPanel(state, writer) {
  const inputs = state.inputs.map((_, index) => inputCard(index, writer));
  const outputs = state.outputs.map((_, index) => outputCard(index, writer, state.limits));
  const strips = state.mixer.map((channel, index) => mixerStrip(index, channel, writer, state.limits));
  const parts = [...inputs, ...outputs, ...strips];
  return {
    pages: {
      inputs: page('Inputs', 'Preamps, input type and per-channel processing',
        h('div', { class: 'card-row' }, inputs.map((part) => part.element))),
      outputs: page('Outputs', 'Output level, source routing and monitoring',
        h('div', { class: 'card-row' }, outputs.map((part) => part.element))),
      mixer: page('Mixer', 'Mix levels for the inputs, the software return and the master bus',
        h('div', { class: 'mixer-grid' }, strips.map((part) => part.element))),
    },
    update(next) {
      parts.forEach((part) => part.update(next));
    },
  };
}

function offlinePage(error, retry) {
  const access = error.code === 'access';
  const steps = access
    ? [
        'Install the udev rule from the package (70-apogee-duet.rules)',
        'Reload the rules with sudo udevadm control --reload-rules',
        'Unplug and replug the Duet so the new permissions apply',
      ]
    : [
        'Connect the Duet over USB and power it on',
        'Confirm the device is listed by lsusb as 0c60:0016',
        'Grant USB access with the udev rule if opening it fails',
      ];
  const shell = card({ title: access ? 'USB access denied' : 'No Apogee Duet found', className: 'offline-card' });
  shell.element.append(
    caption('Checklist'),
    h('ul', { class: 'checklist' }, steps.map((step) => h('li', {}, step))),
    h('div', { class: 'offline-actions' },
      h('button', { type: 'button', class: 'button', onClick: retry }, 'Try again'),
      h('span', { class: 'hint' }, 'Retrying automatically')));
  return page('Device unavailable', error.message, shell.element);
}

class App {
  #panel = null;
  #page = 'inputs';
  #retryTimer = 0;
  #pollTimer = 0;
  #toastTimer = 0;
  #content = document.getElementById('content');
  #nav = document.getElementById('nav');
  #resync = document.getElementById('resync');

  #writer = new Writer({
    onIdle: (state) => this.#panel?.update(state),
    onError: (error) => this.#handleError(error),
  });

  start() {
    this.#nav.addEventListener('click', (event) => {
      const item = event.target.closest('[data-page]');
      if (item) this.#show(item.dataset.page);
    });
    this.#resync.addEventListener('click', () => this.connect());
    const appWindow = getCurrentWindow();
    document.getElementById('minimize').addEventListener('click', () => appWindow.minimize());
    document.getElementById('close').addEventListener('click', () => appWindow.close());
    document.addEventListener('keydown', (event) => {
      const pages = { 1: 'inputs', 2: 'outputs', 3: 'mixer' };
      if (event.ctrlKey && pages[event.key] && this.#panel) {
        event.preventDefault();
        this.#show(pages[event.key]);
      }
    });
    document.addEventListener('contextmenu', (event) => event.preventDefault());
    window.addEventListener('beforeunload', () => this.#writer.flush());
    this.connect();
  }

  async connect() {
    clearTimeout(this.#retryTimer);
    this.#resync.disabled = true;
    await this.#writer.flush();
    try {
      this.#online(await invoke('connect'));
    } catch (error) {
      this.#offline(toError(error));
    } finally {
      this.#resync.disabled = false;
    }
  }

  #online(state) {
    if (!this.#panel) {
      this.#panel = buildPanel(state, this.#writer);
      this.#content.replaceChildren(...Object.values(this.#panel.pages));
      this.#nav.hidden = false;
      this.#resync.hidden = false;
      this.#show(this.#page);
      this.#pollTimer = setInterval(() => {
        if (!document.hidden) this.#writer.poll();
      }, POLL_INTERVAL);
    }
    this.#panel.update(state);
    const label = [state.product || 'Duet USB', state.serial].filter(Boolean).join('  ·  ');
    document.getElementById('device-label').textContent = label;
    this.#status('Connected', 'ok');
  }

  #offline(error) {
    clearInterval(this.#pollTimer);
    this.#panel = null;
    this.#writer.reset();
    this.#nav.hidden = true;
    this.#resync.hidden = true;
    document.getElementById('device-label').textContent = 'USB control panel';
    this.#content.replaceChildren(offlinePage(error, () => this.connect()));
    this.#status('Disconnected', 'danger');
    this.#retryTimer = setTimeout(() => this.connect(), RETRY_INTERVAL);
  }

  #handleError(error) {
    if (OFFLINE_CODES.has(error.code)) {
      this.#offline(error);
      return;
    }
    // The write may or may not have landed: show what the device holds now.
    this.#toast(error.message);
    this.connect();
  }

  #show(name) {
    this.#page = name;
    for (const [key, element] of Object.entries(this.#panel.pages)) element.hidden = key !== name;
    const keepFocus = this.#nav.contains(document.activeElement);
    for (const item of this.#nav.querySelectorAll('[data-page]')) {
      if (item.dataset.page !== name) {
        item.removeAttribute('aria-current');
        continue;
      }
      item.setAttribute('aria-current', 'page');
      // Keyboard shortcuts move the focus ring along with the page.
      if (keepFocus) item.focus();
    }
  }

  #status(text, tone) {
    document.getElementById('status-text').textContent = text;
    document.getElementById('status').dataset.tone = tone;
  }

  #toast(message) {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(this.#toastTimer);
    this.#toastTimer = setTimeout(() => {
      toast.hidden = true;
    }, 4000);
  }
}

new App().start();
