/**
 * Plain-language definitions for every technical term the site uses.
 *
 * One source, so a term means the same thing on every page. Written for the
 * reader who arrives without the vocabulary -- a building owner, an installer,
 * an examiner from outside the field -- which is most readers. A figure they
 * cannot interpret is a figure that does not inform them, however correct it
 * is.
 *
 * Each entry answers "what is it" in one sentence, then "why it matters here"
 * in one more. Nothing longer: a tooltip is read in passing.
 */
export const GLOSSARY = {
  "specific yield": {
    term: "Specific yield",
    define:
      "Electricity produced per kilowatt of installed panels per year, in kWh/kWp.",
    why: "It cancels out system size, so two roofs of different areas can be compared directly.",
  },
  "clearness index": {
    term: "Clearness index",
    define:
      "Sunlight measured at the ground divided by sunlight arriving at the top of the atmosphere.",
    why: "Near 0.75 under a clear sky, below 0.3 under heavy cloud. It is the main predictor of how light is split between direct and scattered.",
  },
  "beam fraction": {
    term: "Beam fraction",
    define: "The share of sunlight arriving straight from the sun’s disc.",
    why: "Only this part casts shadows, so it sets how much a neighbouring building can cost you.",
  },
  "diffuse fraction": {
    term: "Diffuse fraction",
    define:
      "The share of sunlight scattered by the sky, arriving from every direction at once.",
    why: "Shadows do not block it, but tall surroundings do, by hiding part of the sky.",
  },
  "sky view factor": {
    term: "Sky view factor",
    define: "The fraction of the sky dome visible from the roof, from 0 to 1.",
    why: "1.0 is an open horizon. Surrounding buildings lower it and cut the scattered light the panels receive.",
  },
  "heat island": {
    term: "Urban heat island",
    define: "Cities run hotter than the countryside around them.",
    why: "Panels lose about 0.4% of output for every degree they warm, so a hot city costs yield.",
  },
  soiling: {
    term: "Soiling",
    define: "Dust settling on panels and blocking light.",
    why: "It builds up during dry spells and is washed off by rain, so it is worst in arid cities between monsoons.",
  },
  aod: {
    term: "Aerosol optical depth",
    define:
      "How strongly haze, dust and smoke dim sunlight passing through the air.",
    why: "High over the Indo-Gangetic plain. It drives both the scattered-light share and how fast dust settles.",
  },
  era5: {
    term: "ERA5-Land",
    define:
      "A global reconstruction of past weather from the European Centre for Medium-Range Weather Forecasts.",
    why: "The irradiance source here. Its grid is about 9 km, so every roof in one cell receives the same sunlight.",
  },
  modis: {
    term: "MODIS",
    define: "An imaging instrument on NASA’s Terra and Aqua satellites.",
    why: "Supplies the land-surface temperature and aerosol measurements, at 1 km resolution.",
  },
  "performance ratio": {
    term: "Performance ratio",
    define:
      "The share of a panel’s rated output a real system delivers after heat, wiring and inverter losses.",
    why: "Typical Indian systems run 0.78 to 0.83 in their first year.",
  },
  kwp: {
    term: "kWp",
    define:
      "Kilowatt-peak: a panel’s rated power under standard test conditions.",
    why: "The unit installers quote system size in.",
  },
  erbs: {
    term: "Erbs correlation",
    define:
      "A published 1982 formula estimating the scattered-light share from the clearness index.",
    why: "Validated worldwide for four decades, which makes it the baseline a learned model has to beat.",
  },
  skill: {
    term: "Skill score",
    define:
      "How much lower a model’s error is than a baseline’s: 1 minus the ratio of their errors.",
    why: "0 means no better than the baseline. +0.43 means 43% lower error. Negative means worse.",
  },
  holdout: {
    term: "Spatial and temporal holdout",
    define:
      "Test data drawn from cities and a year the model never saw during training.",
    why: "Neighbouring hours are nearly identical, so testing on shuffled rows would report a score that says nothing about new places.",
  },
  "reference spread": {
    term: "Reference spread",
    define:
      "How far the two free irradiance datasets for India disagree with each other: 10.6% for Delhi.",
    why: "No accuracy claim can honestly be tighter than the disagreement between the references it is checked against.",
  },
  ghi: {
    term: "Global horizontal irradiance",
    define:
      "All the sunlight reaching a flat surface, direct and scattered together, in kWh/m².",
    why: "The raw input to everything else here. Over urban India it runs roughly 1,700 to 2,000 kWh/m² a year.",
  },
  "shadow frequency": {
    term: "Shadow frequency",
    define:
      "The share of sunlit hours a roof spends in a neighbouring building’s shadow.",
    why: "It only removes direct light, so its cost is scaled by the beam fraction.",
  },
  "packing factor": {
    term: "Packing factor",
    define: "The share of a roof’s area that can actually hold panels.",
    why: "Water tanks, stairwells, parapets and access space take the rest. 0.70 is the default here.",
  },
  eecu: {
    term: "EECU",
    define:
      "Earth Engine compute unit: Google’s measure of the processing a query uses.",
    why: "The free tier allows a fixed amount per month, which is why the site limits computations per day.",
  },
};

/** Look up an entry by key or by its display term, case-insensitively. */
export function glossaryEntry(key) {
  if (!key) return null;
  const k = key.toLowerCase();
  return (
    GLOSSARY[k] ??
    Object.values(GLOSSARY).find((entry) => entry.term.toLowerCase() === k) ??
    null
  );
}
