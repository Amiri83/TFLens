/* TFLens Discover progress screen (repository page of the managed app, and the analyzer
   of `tflens serve --dir`). The single Discover button posts its form in the background;
   the server starts the job (one per repository) and this script polls its state into
   the box: the four phases with their outcome, the current phase / step, the elapsed
   time, the live log, Hide and Cancel. Cancel stops the run after its current step,
   before anything is written. Hide keeps the run going and shows a small badge that
   reopens the box (remembered per tab); a reload re-attaches to a running job. Without
   JavaScript the form posts normally and the run goes to the end before the page loads.

   The box (#discover-modal) carries its endpoints in data attributes:
   data-current-url, data-status-url / data-cancel-url (with __ID__), data-csrf (managed:
   form token) or data-api="1" (standalone: X-TFLens header). */
(function () {
  "use strict";

  const POLL_MS = 1000;
  const HIDDEN_KEY = "tflens-discover-hidden";
  const modal = document.getElementById("discover-modal");
  if (!modal) return;
  const $ = (id) => document.getElementById(id);
  const title = $("discover-title");
  const phasesEl = $("discover-phases");
  const stepEl = $("discover-step");
  const elapsedEl = $("discover-elapsed");
  const stateEl = $("discover-state");
  const logEl = $("discover-log");
  const messages = $("discover-messages");
  const cancelBtn = $("discover-cancel");
  const hideBtn = $("discover-hide");
  const closeBtn = $("discover-close");
  const badge = $("discover-badge");
  const csrf = modal.dataset.csrf || "";
  const isApi = modal.dataset.api === "1";
  let jobId = null;
  let timer = null;
  let running = false;
  let result = {};

  const url = (template, id) => template.replace("__ID__", encodeURIComponent(id));

  function post(target, fields) {
    if (isApi) {
      const q = new URLSearchParams(fields).toString();
      return fetch(target + (q ? (target.includes("?") ? "&" : "?") + q : ""),
        { method: "POST", headers: { "X-TFLens": "1" }, credentials: "same-origin" });
    }
    const body = new URLSearchParams({ csrf_token: csrf, ...fields });
    return fetch(target, { method: "POST", body: body, credentials: "same-origin" });
  }

  function fmtElapsed(s) {
    const m = Math.floor(s / 60);
    return m ? `${m}m ${s % 60}s` : `${s}s`;
  }

  function stateLabel(job) {
    if (job.running) return job.cancel_requested ? "cancelling (after the current step)" : "running";
    return { done: "finished", cancelled: "cancelled", error: "failed" }[job.state] || job.state;
  }

  function renderPhases(job) {
    phasesEl.replaceChildren(...(job.phases || []).map((p, i) => {
      const li = document.createElement("li");
      li.className = `phase phase-${p.status.replace(/\s+/g, "-")}${p.name === job.phase && job.running ? " current" : ""}`;
      li.dataset.phase = p.name;
      const label = document.createElement("span");
      label.className = "phase-label";
      label.textContent = `${i + 1}. ${p.label}`;
      const status = document.createElement("span");
      status.className = "phase-status";
      status.textContent = p.detail ? `${p.status} — ${p.detail}` : p.status;
      li.append(label, " ", status);
      return li;
    }));
  }

  function renderMessages(list) {
    messages.replaceChildren(...(list || []).map((m) => {
      const li = document.createElement("li");
      li.className = "flash " + (m.category || "");
      li.append(m.text);
      if (m.link_url) {
        const a = document.createElement("a");
        a.href = m.link_url;
        a.textContent = m.link_text || m.link_url;
        li.append(" — ", a);
      }
      return li;
    }));
  }

  function render(job) {
    running = Boolean(job.running);
    result = job.result || {};
    title.textContent = job.label + (running ? "…" : "");
    renderPhases(job);
    stepEl.textContent = job.step || "";
    elapsedEl.textContent = fmtElapsed(job.elapsed || 0);
    stateEl.textContent = stateLabel(job);
    const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 4;
    logEl.textContent = (job.log || []).join("\n");
    if (atBottom) logEl.scrollTop = logEl.scrollHeight;
    renderMessages(job.messages);
    cancelBtn.hidden = !running;
    cancelBtn.disabled = Boolean(job.cancel_requested);
    hideBtn.hidden = !running;
    closeBtn.hidden = running;
    badge.textContent = running ? `Discover running… ${job.step || ""}` : "";
    if (!running) {
      badge.hidden = true;
      sessionStorage.removeItem(HIDDEN_KEY);
    }
  }

  function open() {
    modal.hidden = false;
    badge.hidden = true;
    sessionStorage.removeItem(HIDDEN_KEY);
  }

  function hide() {
    modal.hidden = true;
    badge.hidden = !running;
    if (jobId && running) sessionStorage.setItem(HIDDEN_KEY, jobId);
  }

  function poll() {
    clearTimeout(timer);
    if (!jobId) return;
    fetch(url(modal.dataset.statusUrl, jobId), { credentials: "same-origin" })
      .then((r) => {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then((d) => {
        render(d.job);
        if (d.job.running) timer = setTimeout(poll, POLL_MS);
      })
      .catch((err) => {
        stateEl.textContent = `lost contact with TFLens (${err.message}); retrying…`;
        timer = setTimeout(poll, POLL_MS * 3);
      });
  }

  function attach(job, show) {
    jobId = job.id;
    render(job);
    if (show) open(); else hide();
    if (job.running) timer = setTimeout(poll, POLL_MS);
  }

  cancelBtn.addEventListener("click", () => {
    if (!jobId) return;
    cancelBtn.disabled = true;
    post(url(modal.dataset.cancelUrl, jobId), {}).then(poll).catch(poll);
  });
  hideBtn.addEventListener("click", hide);
  badge.addEventListener("click", open);
  closeBtn.addEventListener("click", () => {
    if (result.url && !location.pathname.startsWith(result.url)) window.location.href = result.url;
    else window.location.reload();
  });
  document.addEventListener("keydown", (evt) => {
    if (evt.key === "Escape" && !modal.hidden && running) hide();
  });

  function notStarted(text) {
    jobId = null;
    running = false;
    title.textContent = "Discover not started";
    phasesEl.replaceChildren();
    stepEl.textContent = "";
    logEl.textContent = "";
    renderMessages([{ category: "error", text: text }]);
    cancelBtn.hidden = true;
    hideBtn.hidden = true;
    closeBtn.hidden = false;
    open();
  }

  // The Discover form(s): posted in the background, followed in the box.
  document.querySelectorAll("form[data-discover]").forEach((form) => {
    form.addEventListener("submit", (evt) => {
      evt.preventDefault();
      const fields = Object.fromEntries(new FormData(form).entries());
      delete fields.csrf_token;
      fields.background = "1";
      const buttons = form.querySelectorAll("button");
      buttons.forEach((b) => { b.disabled = true; });
      post(form.getAttribute("action"), fields)
        .then((r) => r.json().then((d) => ({ status: r.status, d: d })))
        .then(({ status, d }) => {
          if (d.job) {
            attach(d.job, true);
            if (!d.ok) renderMessages([{ category: "error", text: d.error }]);
            return;
          }
          notStarted(d.error || `HTTP ${status}`);
        })
        .catch((err) => notStarted(`Could not start Discover (${err.message}).`))
        .finally(() => buttons.forEach((b) => { b.disabled = false; }));
    });
  });

  // Re-attach after a reload: the running job of this repository (kept hidden if hidden).
  fetch(modal.dataset.currentUrl, { credentials: "same-origin" })
    .then((r) => (r.ok ? r.json() : { job: null }))
    .then((d) => {
      const job = d.job;
      if (job && job.running) attach(job, sessionStorage.getItem(HIDDEN_KEY) !== job.id);
      if (d.aws) {  // standalone: whether the AWS phase can be turned on
        const box = document.getElementById("discover-aws");
        const why = document.getElementById("discover-aws-why");
        if (box) box.disabled = !d.aws.available;
        if (why) why.textContent = d.aws.available ? `(${d.aws.label})` : `(${d.aws.reason})`;
      }
    })
    .catch(() => {});
})();
