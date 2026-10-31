# Vendored front-end libraries

`tflens serve` makes no external network requests, so third-party JavaScript is
copied here and served locally.

- `cytoscape.min.js` — [Cytoscape.js](https://js.cytoscape.org/) (MIT). Copy the
  minified build into this directory; `index.html` loads it from
  `static/vendor/cytoscape.min.js`. Without it, the graph view shows a notice and
  the repository view and detail panel still work.
