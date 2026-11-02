# Vendored front-end libraries

`tflens serve` makes no external network requests, so third-party JavaScript is
copied here and served locally (no CDN, no remote font or style sheet at runtime).

- `cytoscape.min.js` — [Cytoscape.js](https://js.cytoscape.org/) (MIT, see
  `cytoscape.LICENSE`). The canvas of the Graph and of every diagram; `index.html`
  loads it from `static/vendor/cytoscape.min.js`. Without it, the graph view shows a
  notice, the diagrams fall back to their static SVG, and the repository view and
  detail panel still work.
- `dagre.min.js` — [dagre](https://github.com/dagrejs/dagre) (MIT, see
  `dagre.LICENSE`). The diagram toolbar's "Tidy" automatic layout
  (`static/diagram.js`); without it, "Tidy" says so and everything else works.

The diagram component itself (`static/diagram.js`: toolbar, editing, saved layouts,
PNG / SVG / draw.io export) is TFLens's own code, modelled on IPLens's diagram page.
