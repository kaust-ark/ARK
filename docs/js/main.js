/* ===== Scroll Progress Bar ===== */
const scrollProgress = document.getElementById('scrollProgress');
function updateScrollProgress() {
  const scrollTop = window.scrollY;
  const docHeight = document.documentElement.scrollHeight - window.innerHeight;
  const progress = docHeight > 0 ? (scrollTop / docHeight) * 100 : 0;
  scrollProgress.style.width = progress + '%';
}
window.addEventListener('scroll', updateScrollProgress, { passive: true });

/* ===== Navbar scroll effect ===== */
const navbar = document.getElementById('navbar');
window.addEventListener('scroll', () => {
  navbar.classList.toggle('scrolled', window.scrollY > 40);
}, { passive: true });

/* ===== Back to Top Button ===== */
const backToTop = document.getElementById('backToTop');
window.addEventListener('scroll', () => {
  const heroHeight = document.getElementById('hero').offsetHeight;
  backToTop.classList.toggle('visible', window.scrollY > heroHeight);
}, { passive: true });
backToTop.addEventListener('click', () => {
  window.scrollTo({ top: 0, behavior: 'smooth' });
});

/* ===== Mobile hamburger menu ===== */
const hamburger = document.getElementById('hamburger');
const navLinks = document.getElementById('navLinks');
hamburger.addEventListener('click', () => {
  navLinks.classList.toggle('open');
});
navLinks.querySelectorAll('a').forEach(link => {
  link.addEventListener('click', () => navLinks.classList.remove('open'));
});

/* ===== Smooth scroll with offset for sticky navbar ===== */
document.querySelectorAll('a[href^="#"]').forEach(a => {
  a.addEventListener('click', e => {
    const target = document.querySelector(a.getAttribute('href'));
    if (target) {
      e.preventDefault();
      const offset = 80;
      const top = target.getBoundingClientRect().top + window.scrollY - offset;
      window.scrollTo({ top, behavior: 'smooth' });
    }
  });
});

/* ===== Scroll Reveal (IntersectionObserver) ===== */
const revealObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.classList.add('visible');
      revealObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.1 });

document.querySelectorAll('.reveal').forEach(el => revealObserver.observe(el));

/* ===== Floating Particles ===== */
function createParticles() {
  const container = document.getElementById('heroParticles');
  if (!container) return;
  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (prefersReducedMotion) return;

  const count = 40;
  for (let i = 0; i < count; i++) {
    const particle = document.createElement('div');
    particle.classList.add('particle');
    const size = Math.random() * 3 + 2;
    particle.style.width = size + 'px';
    particle.style.height = size + 'px';
    particle.style.left = Math.random() * 100 + '%';
    particle.style.animationDuration = (Math.random() * 15 + 10) + 's';
    particle.style.animationDelay = (Math.random() * 10) + 's';
    particle.style.opacity = Math.random() * 0.3 + 0.1;
    container.appendChild(particle);
  }
}
createParticles();

/* ===== Terminal Typing Effect (Enhanced) ===== */
const terminalLines = [
  { type: 'prompt', text: '$ ' },
  { type: 'command', text: 'git clone https://github.com/kaust-ark/ARK.git && cd ARK', delay: 25 },
  { type: 'output', text: '\n  Cloning into \'ARK\'...\n\n', delay: 8 },
  { type: 'prompt', text: '$ ' },
  { type: 'command', text: 'pip install -e .', delay: 35 },
  { type: 'output', text: '\n  Successfully installed ark-research-0.1.0\n\n', delay: 8 },
  { type: 'prompt', text: '$ ' },
  { type: 'command', text: 'ark new mma', delay: 35 },
  { type: 'output', text: '\n  Project name: mma', delay: 8 },
  { type: 'output', text: '\n  Research idea: CPU Matrix Multiplication', delay: 8 },
  { type: 'output', text: '\n  Venue: neurips', delay: 8 },
  { type: 'output', text: '\n  Config: projects/mma/config.yaml\n\n', delay: 8 },
  { type: 'prompt', text: '$ ' },
  { type: 'command', text: 'ark run mma', delay: 35 },
  { type: 'output', text: '\n  Starting ARK pipeline...', delay: 8 },
  { type: 'output', text: '\n  [Phase 1/3] Research      \u2713', delay: 8 },
  { type: 'output', text: '\n  [Phase 2/3] Development   \u2713', delay: 8 },
  { type: 'output', text: '\n  [Phase 3/3] Review        \u2713', delay: 8 },
  { type: 'output', text: '\n\n  ', delay: 8 },
  { type: 'highlight', text: '\u2605 Final score: 7.2/10', delay: 25 },
  { type: 'output', text: '\n  PDF: auto_research/output/mma.pdf\n', delay: 8 },
];

function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

async function typeTerminal() {
  const body = document.getElementById('terminalBody');
  if (!body) return;
  body.innerHTML = '';

  for (const line of terminalLines) {
    const span = document.createElement('span');
    span.className = line.type;
    body.appendChild(span);

    for (const char of line.text) {
      span.textContent += char;
      await sleep(line.delay || 25);
    }
  }

  const cursor = document.createElement('span');
  cursor.className = 'cursor';
  body.appendChild(cursor);
}

// Start typing when hero is visible
const heroObserver = new IntersectionObserver((entries) => {
  if (entries[0].isIntersecting) {
    setTimeout(typeTerminal, 600);
    heroObserver.disconnect();
  }
}, { threshold: 0.3 });
heroObserver.observe(document.getElementById('hero'));

/* ===== Animated Counter ===== */
function animateCounters() {
  const counters = document.querySelectorAll('.stat-number[data-target]');
  counters.forEach(counter => {
    if (counter.dataset.animated) return;
    counter.dataset.animated = 'true';
    const target = parseInt(counter.dataset.target);
    const suffix = counter.dataset.suffix || '';
    const duration = 1500;
    const startTime = performance.now();

    function update(currentTime) {
      const elapsed = currentTime - startTime;
      const progress = Math.min(elapsed / duration, 1);
      // Ease out cubic
      const eased = 1 - Math.pow(1 - progress, 3);
      const current = Math.round(eased * target);
      counter.textContent = current + suffix;
      if (progress < 1) {
        requestAnimationFrame(update);
      } else {
        counter.textContent = target + suffix;
      }
    }
    requestAnimationFrame(update);
  });
}

const statsObserver = new IntersectionObserver((entries) => {
  if (entries[0].isIntersecting) {
    animateCounters();
    statsObserver.disconnect();
  }
}, { threshold: 0.3 });
const statsBar = document.getElementById('statsBar');
if (statsBar) statsObserver.observe(statsBar);

/* ===== Pipeline Phase Animation ===== */
function animatePipeline() {
  const phases = document.querySelectorAll('.phase-card');
  const connectors = document.querySelectorAll('.phase-connector');

  phases.forEach((phase, i) => {
    setTimeout(() => {
      phase.classList.add('visible');
      // Briefly highlight active
      phase.classList.add('active');
      setTimeout(() => {
        if (i < phases.length - 1) phase.classList.remove('active');
      }, 1200);
    }, i * 600);
  });

  connectors.forEach((conn, i) => {
    setTimeout(() => {
      conn.classList.add('animated');
    }, (i + 1) * 600 - 200);
  });
}

const pipelineObserver = new IntersectionObserver((entries) => {
  if (entries[0].isIntersecting) {
    animatePipeline();
    pipelineObserver.disconnect();
  }
}, { threshold: 0.2 });
const pipelineEl = document.getElementById('pipelinePhases');
if (pipelineEl) pipelineObserver.observe(pipelineEl);

/* ===== Review Loop Sequential Highlight ===== */
function animateReviewLoop() {
  const steps = document.querySelectorAll('.loop-step');
  let currentStep = 0;

  function highlightNext() {
    steps.forEach(s => s.classList.remove('active'));
    if (steps[currentStep]) {
      steps[currentStep].classList.add('active');
    }
    currentStep = (currentStep + 1) % steps.length;
  }

  highlightNext();
  setInterval(highlightNext, 2000);
}

const loopObserver = new IntersectionObserver((entries) => {
  if (entries[0].isIntersecting) {
    animateReviewLoop();
    loopObserver.disconnect();
  }
}, { threshold: 0.3 });
const reviewLoop = document.getElementById('reviewLoop');
if (reviewLoop) loopObserver.observe(reviewLoop);

/* ===== Paper Card Tilt Effect ===== */
document.querySelectorAll('[data-tilt]').forEach(card => {
  card.addEventListener('mousemove', e => {
    const rect = card.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const centerX = rect.width / 2;
    const centerY = rect.height / 2;
    const rotateX = ((y - centerY) / centerY) * -4;
    const rotateY = ((x - centerX) / centerX) * 4;
    card.style.transform = `perspective(800px) rotateX(${rotateX}deg) rotateY(${rotateY}deg) translateY(-6px)`;
  });

  card.addEventListener('mouseleave', () => {
    card.style.transform = 'perspective(800px) rotateX(0) rotateY(0) translateY(0)';
  });
});

/* ===== Quick Start Line Highlight ===== */
const qsObserver = new IntersectionObserver((entries) => {
  if (entries[0].isIntersecting) {
    const lines = document.querySelectorAll('.quickstart-terminal .cmd[data-line]');
    lines.forEach((line, i) => {
      setTimeout(() => {
        line.classList.add('highlight-line');
        setTimeout(() => line.classList.remove('highlight-line'), 800);
      }, i * 400);
    });
    qsObserver.disconnect();
  }
}, { threshold: 0.4 });
const qsTerminal = document.querySelector('.quickstart-terminal');
if (qsTerminal) qsObserver.observe(qsTerminal);

/* ===== Copy Button with Animated Checkmark ===== */
const copyBtn = document.getElementById('copyBtn');
if (copyBtn) {
  copyBtn.addEventListener('click', () => {
    const commands = `git clone https://github.com/kaust-ark/ARK.git && cd ARK\npip install -e .\nark new mma\nark run mma\nark monitor mma`;
    navigator.clipboard.writeText(commands).then(() => {
      copyBtn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5"><polyline class="checkmark-path" points="20 6 9 17 4 12"/></svg> Copied!`;
      copyBtn.style.color = '#2dd4bf';
      copyBtn.style.borderColor = '#2dd4bf';
      setTimeout(() => {
        copyBtn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" fill="none" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg> Copy`;
        copyBtn.style.color = '';
        copyBtn.style.borderColor = '';
      }, 2000);
    });
  });
}

/* ===== Roadmap Timeline Drawing ===== */
function animateRoadmap() {
  const timeline = document.getElementById('roadmapTimeline');
  const line = document.getElementById('roadmapLine');
  const items = document.querySelectorAll('[data-roadmap]');
  if (!timeline || !line || !items.length) return;

  const itemObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');

        // Update line height to reach this item
        const timelineRect = timeline.getBoundingClientRect();
        const itemRect = entry.target.getBoundingClientRect();
        const itemBottom = itemRect.top - timelineRect.top + itemRect.height;
        const currentHeight = parseInt(line.style.height) || 0;
        if (itemBottom > currentHeight) {
          line.style.height = itemBottom + 'px';
        }

        itemObserver.unobserve(entry.target);
      }
    });
  }, { threshold: 0.3 });

  items.forEach(item => itemObserver.observe(item));
}
animateRoadmap();
