#pragma once

const char DASHBOARD[] PROGMEM = R"HTML(
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Job radar</title>
  <style>
    *{box-sizing:border-box} body{margin:0;background:#101b28;color:#eaf1f7;font:16px/1.5 system-ui,sans-serif}
    main{max-width:860px;margin:auto;padding:28px 18px} h1{font-size:clamp(2rem,7vw,3rem);margin:.1em 0}
    .eyebrow{color:#71dfbd;text-transform:uppercase;letter-spacing:.14em;font-size:.75rem;font-weight:bold}
    .intro,.status,.meta{color:#b4c4d2} .status{margin:20px 0;font-size:.9rem}
    .controls{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0} button{background:#203449;color:#eaf1f7;border:1px solid #3b5365;border-radius:20px;padding:8px 14px;cursor:pointer}
    button.active{background:#71dfbd;color:#10271f;border-color:#71dfbd} .job{background:#1b2b3c;border:1px solid #2e465b;border-radius:16px;padding:18px;margin:12px 0}
    h2{font-size:1.15rem;margin:5px 0} .tag{font-size:.77rem;color:#71dfbd;font-weight:700;text-transform:uppercase} a{color:#8ee4ce;overflow-wrap:anywhere}
    footer{margin:28px 0;color:#9eb0bd;font-size:.8rem} .warning{color:#ffd08c}
  </style>
</head>
<body><main>
  <div class="eyebrow">Personal job radar</div><h1>Find your next role.</h1>
  <p class="intro">AI engineering and internships · C#/.NET · Europe and northern Portugal</p>
  <p class="status" id="status" role="status">Loading jobs…</p>
  <div class="controls" id="filters" aria-label="Filter jobs"></div>
  <section id="jobs" aria-live="polite"></section>
  <footer>Listings via <a href="https://remotive.com/">Remotive</a>, <a href="https://www.arbeitnow.com/">Arbeitnow</a> and <a href="https://jobicy.com/">Jobicy</a>. Confirm eligibility and whether local roles offer hybrid work with the employer.</footer>
</main>
<script>
  const jobsNode = document.getElementById('jobs');
  const statusNode = document.getElementById('status');
  const filtersNode = document.getElementById('filters');
  let jobs = [], active = 'All';
  const filters = ['All', 'AI engineering', 'AI internship', '.NET engineering'];
  function render() {
    filtersNode.replaceChildren();
    filters.forEach(name => {
      const b = document.createElement('button'); b.textContent = name;
      b.className = name === active ? 'active' : ''; b.setAttribute('aria-pressed', name === active);
      b.onclick = () => { active = name; render(); }; filtersNode.append(b);
    });
    jobsNode.replaceChildren();
    const visible = jobs.filter(j => active === 'All' || j.category === active);
    if (!visible.length) { jobsNode.textContent = 'No matching jobs in this category yet.'; return; }
    visible.forEach(job => {
      const card = document.createElement('article'); card.className = 'job';
      const tag = document.createElement('div'); tag.className = 'tag'; tag.textContent = job.category;
      const title = document.createElement('h2'); title.textContent = job.title;
      const company = document.createElement('div'); company.textContent = job.company;
      const meta = document.createElement('p'); meta.className = 'meta';
      meta.textContent = `${job.location} · ${job.work_mode} · Posted ${job.published_at.slice(0, 10)} · ${job.source}`;
      const link = document.createElement('a');
      try {
        const url = new URL(job.url);
        if (url.protocol === 'https:' && ['remotive.com', 'www.arbeitnow.com', 'arbeitnow.com', 'jobicy.com'].includes(url.hostname)) {
          link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = `View on ${job.source} ↗`;
        }
      } catch (_) {}
      card.append(tag, title, company, meta, link); jobsNode.append(card);
    });
  }
  async function load() {
    try {
      const response = await fetch('/api/jobs', {cache: 'no-store'});
      if (!response.ok) throw new Error(await response.text());
      const feed = await response.json(); jobs = feed.jobs;
      const failed = Object.entries(feed.sources).filter(([,v]) => !v.ok).map(([k]) => k);
      const stale = response.headers.get('X-Fetch-Status') === 'stale';
      statusNode.className = stale || failed.length ? 'status warning' : 'status';
      statusNode.textContent = `${jobs.length} recent matches · Search updated ${new Date(feed.generated_at).toLocaleString()}${stale ? ' · ESP32 feed refresh failed; showing cached results' : ''}${failed.length ? ' · Source unavailable: ' + failed.join(', ') : ''}`;
      render();
    } catch (error) { statusNode.className = 'status warning'; statusNode.textContent = `Feed unavailable: ${error.message}`; }
  }
  load(); setInterval(load, 60000);
</script></body></html>
)HTML";
