/* ============================================================
   petals — sakura falling behind the interface.

   Ornament, and treated as ornament: it stops entirely under
   prefers-reduced-motion, pauses when the tab is hidden, and
   scales its own count down on a small screen. A control panel
   is somewhere people work, so the decoration is not allowed to
   cost them a frame while a 300-mod list is rendering.
   ============================================================ */

const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)');

export function startPetals(canvas) {
  if (!canvas || REDUCED.matches) return () => {};
  const ctx = canvas.getContext('2d', { alpha: true });
  if (!ctx) return () => {};

  let width = 0;
  let height = 0;
  let dpr = 1;
  let petals = [];
  let raf = null;
  let last = performance.now();

  const colours = ['#E9A0B4', '#F3C3CE', '#D9788F', '#F8DCE3'];

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const target = width < 700 ? 12 : width < 1400 ? 22 : 32;
    while (petals.length < target) petals.push(spawn(true));
    if (petals.length > target) petals.length = target;
  }

  function spawn(anywhere) {
    return {
      x: Math.random() * width,
      y: anywhere ? Math.random() * height : -20,
      size: 5 + Math.random() * 7,
      speed: 14 + Math.random() * 26,
      drift: (Math.random() - 0.5) * 22,
      spin: (Math.random() - 0.5) * 1.6,
      angle: Math.random() * Math.PI * 2,
      colour: colours[(Math.random() * colours.length) | 0],
      alpha: 0.35 + Math.random() * 0.45,
    };
  }

  function frame(now) {
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    ctx.clearRect(0, 0, width, height);
    for (let i = 0; i < petals.length; i++) {
      const p = petals[i];
      p.y += p.speed * dt;
      p.x += Math.sin(p.angle) * p.drift * dt;
      p.angle += p.spin * dt;
      if (p.y > height + 24) petals[i] = spawn(false);

      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.angle);
      ctx.globalAlpha = p.alpha;
      ctx.fillStyle = p.colour;
      // A single petal: two arcs meeting at a notch, which reads as sakura
      // at eight pixels far better than a circle does.
      ctx.beginPath();
      ctx.moveTo(0, -p.size);
      ctx.quadraticCurveTo(p.size * 0.72, -p.size * 0.3, 0, p.size);
      ctx.quadraticCurveTo(-p.size * 0.72, -p.size * 0.3, 0, -p.size);
      ctx.fill();
      ctx.restore();
    }
    raf = requestAnimationFrame(frame);
  }

  function start() {
    if (raf !== null) return;
    last = performance.now();
    raf = requestAnimationFrame(frame);
  }
  function stop() {
    if (raf === null) return;
    cancelAnimationFrame(raf);
    raf = null;
    ctx.clearRect(0, 0, width, height);
  }

  resize();
  start();
  window.addEventListener('resize', resize, { passive: true });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stop(); else start();
  });
  REDUCED.addEventListener?.('change', (e) => {
    if (e.matches) stop(); else start();
  });

  return stop;
}
