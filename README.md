# Civic Assembly panel: open data + embeddable charts

Live: https://rengels-coc.github.io/civic-assembly-panel/ · embed codes: https://rengels-coc.github.io/civic-assembly-panel/embed-codes.html

Turns the Panelot lottery result into:

- `site/data/panel_composition.csv` (and `.json`): one long table (category, group, count, percent, target min/max). Upload it to Socrata as a single dataset.
- `site/index.html`: all charts on one page.
- `site/embed/<category>.html`: one chart per category, for iframes on the City site. Each page reports its height to the parent page via `postMessage`.
- `site/embed/all.html`: **recommended**. Every chart in one iframe. `embed-codes.html` has the snippet and an optional auto-size script, and `host-demo.html` shows it on a mock City page.
- Freshness: GitHub Pages lets browsers cache pages for 10 minutes. Every page checks `data/version.json`, which bypasses the cache, on load and every 2 minutes while visible. If there's a newer build, the page reloads itself, so the City site never needs to change URLs or cache-bust. PNGs aren't covered, and copies uploaded into the CMS never update.
- `site/charts/<category>.png`: static images for CMS pages that block iframes.
- `site/embed-codes.html`: copy-paste iframe and `<img>` snippets.

Only aggregate counts are published. The `Black(+) x Neighborhood` quota helper is hidden (`show=0` in `config/categories.csv`) because its cells are very small.

## Tonight's runbook

1. Get the result in **any** of these forms and save it in `input/`:
   - the printed Panelot `Panel Draw` PDF. `extract_pdf.py` reads the pie slices' vector shapes, which give exact counts. It checks that the pies are complete, the counts are whole numbers, the totals are 40, and every count is within its target. If anything looks wrong it prints `PDF PROBLEM`. **Compare the charts with the Panelot screen before you publish.** The slice-to-label mapping depends on Panelot's value order (`panelot_order` in the config), so tied counts can't confirm it.
  
   - a per-panelist CSV or XLSX (one row per person, one column per category), **or**
   - category / value / count, **or**
   - category / value / percent (as on the Panelot screen). If that's all you have, fill in `input/manual_counts_template.csv`.
   
   Wording can be the registration wording or the Panelot display wording.
2. Run `python build.py input\<file> --date 2026-10-05 --base-url https://rengels-coc.github.io/civic-assembly-panel --strict`. Do not pass `--banner`, because that adds the TEST DATA strip.
   - Any `UNRECOGNIZED` or `WARNING` lines mean a label mismatch or a total that isn't 40. Fix them before you publish. Add `--strict` to stop on warnings.
   - Use `--no-targets` to hide the target ranges (bar style only).
   - Charts are Panelot-style pies by default: same colors, slice order, and rounding as the Panelot screen, so visual QA is a side-by-side comparison. Below 520px wide they switch to a pie plus a color legend. `--style bar` gives the older bar charts with target-range bands.
   - Panelot's 9th and 10th colors don't print in the PDF. The pies assume the palette wraps (9th = blue, 10th = orange), based on the label halos in the screenshot. This only affects West Cambridge and North Cambridge.
3. Check `site/index.html` locally, then run `git add -A; git commit -m "Live results"; git push`. Pages redeploys in about a minute. The browser and CDN may cache the old version for up to 10 minutes.
4. Optional: upload `site/data/panel_composition.csv` to data.cambridgema.gov.

## Before the draw

- Run `python build.py --placeholder --date 2026-10-05` and push. The embed URLs stay the same, so the website can embed them now and they fill in when you push the results.
- To get iframe heights, use `site/embed-codes.html` from a run with real or sample data. Placeholder heights are too short.

## Test fixtures

`input/test/` has the sample screenshot's numbers in three formats. All three must produce identical output:
`sample_counts.csv`, `sample_percents.csv` (Panelot labels), and `sample_panelists.xlsx` (synthetic people).
`tests/host_test.html` is a mock host page that auto-sizes the iframes.

