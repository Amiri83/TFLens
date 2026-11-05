/* Runs static/app.js under node with a small DOM and canned API responses, and prints what
   it rendered as JSON: {text, errors, unanswered}. No browser is needed; the graph library
   is left out, so the views use the fallback the page has without it.
   Usage: node render_analyzer.js app.js responses.json */
"use strict";
const fs = require("fs");
const vm = require("vm");

const responses = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const byId = new Map();
const errors = [];
const unanswered = [];

class Node_ {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attrs = {};
    this.dataset = {};
    this.hidden = false;
    this.value = "";
    this.className = "";
    this.style = new Proxy({}, { get: (o, k) => (k === "setProperty" ? () => {} : o[k]), set: (o, k, v) => { o[k] = v; return true; } });
    this.classList = {
      add: () => {}, remove: () => {}, toggle: () => {}, contains: () => false,
    };
    this.listeners = {};
    this.parentElement = null;
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k === "id") byId.set(String(v), this);
  }
  getAttribute(k) { return this.attrs[k] ?? null; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  removeEventListener() {}
  append(...c) { for (const x of c) if (x !== null && x !== undefined) this.children.push(x); }
  appendChild(c) { this.children.push(c); return c; }
  prepend(...c) { this.children.unshift(...c); }
  replaceChildren(...c) { this.children = c.filter((x) => x !== null && x !== undefined); }
  remove() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  scrollIntoView() {}
  getBoundingClientRect() { return { width: 800, height: 600, left: 0, top: 0 }; }
  set textContent(v) { this.children = [String(v)]; }
  get textContent() { return text(this); }
  set innerHTML(v) { this.children = [String(v)]; }
  get clientWidth() { return 800; }
  get clientHeight() { return 600; }
}

function text(n) {
  if (typeof n === "string") return n;
  const own = ["title", "aria-label"].map((k) => n.attrs[k]).filter(Boolean).join(" ");
  return [own, ...n.children.map(text)].join(" ");
}

function element(id) {
  if (!byId.has(id)) {
    const n = new Node_("div");
    n.attrs.id = id;
    byId.set(id, n);
  }
  return byId.get(id);
}

const ready = [];
const sandbox = {
  console,
  URLSearchParams,
  URL,
  encodeURIComponent,
  decodeURIComponent,
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  location: { search: "", pathname: "/runs/1/", href: "" },
  requestAnimationFrame: (f) => setTimeout(f, 0),
  setTimeout,
  clearTimeout,
  ResizeObserver: class { observe() {} disconnect() {} },
  document: {
    documentElement: new Node_("html"),
    body: new Node_("body"),
    getElementById: element,
    createElement: (tag) => new Node_(tag),
    createElementNS: (_ns, tag) => new Node_(tag),
    createTextNode: (t) => String(t),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: (t, f) => { if (t === "DOMContentLoaded") ready.push(f); },
  },
  fetch: async (path) => {
    const url = new URL(String(path), "http://example.invalid/");
    const query = [...url.searchParams.entries()].filter(([, v]) => v !== "").sort()
      .map(([k, v]) => `${k}=${v}`).join("&");
    const key = url.pathname.slice(1) + (query ? `?${query}` : "");
    const known = responses[key] ?? responses[url.pathname.slice(1)];
    if (known === undefined && !url.pathname.endsWith("/diagram") && !url.pathname.endsWith("/layout")) {
      unanswered.push(key);
    }
    const body = known ?? "{}";
    return { ok: known !== undefined, status: known === undefined ? 404 : 200, statusText: "absent",
      json: async () => JSON.parse(body), text: async () => body };
  },
};
sandbox.window = sandbox;
process.on("unhandledRejection", (e) => errors.push(String(e && e.stack || e)));

vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), sandbox);
for (const f of ready) f();

setTimeout(() => {
  const failed = element("detail").children.map(text).join(" ");
  if (failed.includes("Failed to load")) errors.push(failed);
  const shown = ["environments", "env-toc", "roots", "roots-toc", "flow", "flow-toc", "overview", "toc",
    "diagrams", "summary", "summary-details", "root-list"];
  console.log(JSON.stringify({
    text: shown.map((id) => text(element(id))).join("\n"),
    errors,
    unanswered,
  }));
}, 500);
