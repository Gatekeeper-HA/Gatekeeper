// The Zones page (zones.py): pick a zone, click its corners on the picture in order.
// Without it the outlines still show, and the points can be typed.
const svg = document.getElementById('shapes');
const inputs = [...document.querySelectorAll('input[data-zone]')];
function pick() { return document.querySelector('input[name=draw]:checked')?.value; }
function points(text) {
  return text.trim().split(/[\s;]+/).filter(Boolean).map(p => p.split(',').map(Number))
    .filter(p => p.length === 2 && p.every(v => v >= 0 && v <= 1));
}
function redraw() {
  for (const input of inputs) {
    const shape = document.getElementById('shape-' + input.dataset.zone);
    const text = input.value.trim() || input.placeholder;  // blank: Frigate's
    shape.setAttribute('points', points(text).map(p => p.join(',')).join(' '));
    shape.classList.toggle('picked', input.dataset.zone === pick());
  }
}
document.getElementById('frame').addEventListener('click', e => {
  const zone = pick(); if (!zone) return;
  const r = e.currentTarget.getBoundingClientRect();
  const x = ((e.clientX - r.left) / r.width).toFixed(3);
  const y = ((e.clientY - r.top) / r.height).toFixed(3);
  const input = inputs.find(i => i.dataset.zone === zone);
  input.value = (input.value.trim() + ' ' + x + ',' + y).trim(); redraw();
});
for (const b of document.querySelectorAll('button[data-undo]')) b.addEventListener('click', () => {
  const input = inputs.find(i => i.dataset.zone === b.dataset.undo);
  input.value = input.value.trim().split(/\s+/).slice(0, -1).join(' '); redraw();
});
for (const b of document.querySelectorAll('button[data-clear]')) b.addEventListener('click', () => {
  inputs.find(i => i.dataset.zone === b.dataset.clear).value = ''; redraw();
});
for (const el of [...inputs, ...document.querySelectorAll('input[name=draw]')])
  el.addEventListener('input', redraw);
for (const b of document.querySelectorAll('.drawonly')) b.hidden = false;
redraw();
